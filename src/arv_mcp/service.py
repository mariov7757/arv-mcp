"""
service.py — address in, ARV evidence out. Everything the MCP tools do,
as plain synchronous Python (server.py runs it in worker threads).

    get_arv(address, **facts, latitude?, longitude?, radius_mi?, days_back?,
            include_market_context?, max_comps?)  -> dict
    lookup_property(address)                      -> dict

Both raise ArvError with a machine-readable `code` (ADDRESS_NOT_FOUND,
ADDRESS_NOT_VERIFIED, MISSING_FACTS, UNSUPPORTED_PROPERTY_TYPE, NO_COMPS,
RATE_LIMITED, UPSTREAM_ERROR, INVALID_INPUT) so the model can act on it.

Subject resolution:
  1. Verified record: Realtor.com's record for exactly this street + unit
     (data/homeharvest.resolve_record), whatever its status. Caller facts
     override record fields and are listed in `overridden`.
  2. Otherwise locate it — caller lat/lon, then the Census geocoder, then
     Realtor.com's own geocoder when it landed on the same street (a
     building-level point) — and build the subject from caller facts, which
     must include sqft and property_type. No location -> ADDRESS_NOT_FOUND;
     a location but missing facts -> ADDRESS_NOT_VERIFIED telling the model
     exactly what to ask the user for.
Comps are always searched around the subject's coordinates, and the
subject's own sale is pulled out of its comps and reported instead.
"""

import logging
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import arv, scoring
from .comps import (
    PROPERTY_TYPE_ALIASES,
    _subject_street_key,
    fetch_active_pending_comps,
    fetch_comps,
    split_subject,
)
from .data import homeharvest as hh
from .data.client import UpstreamError
from .data.geocode import census_geocode
from .ratelimit import RateLimited

log = logging.getLogger(__name__)

# ── Limits and defaults ───────────────────────────────────────────────────────
RADIUS_MIN_MI, RADIUS_MAX_MI = 0.25, 5.0
DAYS_MIN, DAYS_MAX = 30, 730
DEFAULT_MAX_COMPS, MAX_COMPS_CAP = 10, 25
EXCLUDED_CAP = 15
MARKET_CONTEXT_CAP = 10
CACHE_TTL_S = 15 * 60
CACHE_MAX_ENTRIES = 128

REQUIRED_FACTS = ("sqft", "property_type")
RECOMMENDED_FACTS = ("beds", "baths", "year_built", "lot_sqft", "stories", "garage", "has_pool")
FACT_FIELDS = REQUIRED_FACTS + RECOMMENDED_FACTS

# MLS special listing conditions that mark a sale as not arm's-length.
DISTRESS_CONDITION_WORDS = ("reo", "lender owned", "bank owned", "short sale", "foreclosure",
                            "auction", "hud owned", "corporate owned")
DISTRESSED_SHARE_NOTE = 0.20
DISTRESSED_MEDIAN_GAP_NOTE = 0.05

PROPERTY_TYPE_WARNINGS = {
    "farm": "Farm/ranch value is driven by acreage and improvements, not living sqft; treat the $/sqft estimate as a rough check.",
    "multi_family": "Income properties are usually valued per unit or by rent (cap rate); treat the $/sqft estimate as a rough check only.",
    "mobile": "A mobile home in a park may not include the land, and park and owned-lot homes can mix in the comp pool.",
}

CONFIDENCE_EXPLANATIONS = {
    5: "5+ eligible comps within 0.25 mi",
    4: "5+ eligible comps within 0.5 mi",
    3: "3+ eligible comps within 1 mi",
    2: "at least 1 eligible comp, but the evidence is sparse or far",
    1: "no eligible comps with a known distance",
}


class ArvError(Exception):
    """A tool-level failure the model can act on."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


# ── Cache ─────────────────────────────────────────────────────────────────────

class _TTLCache:
    def __init__(self, ttl_s: float = CACHE_TTL_S, max_entries: int = CACHE_MAX_ENTRIES, clock=time.monotonic):
        self._ttl, self._max, self._clock = ttl_s, max_entries, clock
        self._data: dict = {}
        self._lock = threading.Lock()

    def get_or_set(self, key, factory):
        now = self._clock()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        value = factory()  # outside the lock: factories make network calls
        with self._lock:
            if len(self._data) >= self._max:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (now, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_record_cache = _TTLCache()
_comp_cache = _TTLCache()


def clear_caches() -> None:
    _record_cache.clear()
    _comp_cache.clear()


# ── Inputs ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Window:
    radius_mi: float
    days_back: int
    adjusted: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return self.radius_mi == hh.COMP_RADIUS_MILES and self.days_back == hh.COMP_DAYS


def _window(radius_mi, days_back) -> Window:
    notes = []
    r = hh.COMP_RADIUS_MILES if radius_mi is None else float(radius_mi)
    d = hh.COMP_DAYS if days_back is None else int(days_back)
    if not RADIUS_MIN_MI <= r <= RADIUS_MAX_MI:
        r2 = min(RADIUS_MAX_MI, max(RADIUS_MIN_MI, r))
        notes.append(f"radius_mi {r:g} adjusted to {r2:g} (allowed {RADIUS_MIN_MI:g}-{RADIUS_MAX_MI:g})")
        r = r2
    if not DAYS_MIN <= d <= DAYS_MAX:
        d2 = min(DAYS_MAX, max(DAYS_MIN, d))
        notes.append(f"days_back {d} adjusted to {d2} (allowed {DAYS_MIN}-{DAYS_MAX})")
        d = d2
    return Window(r, d, tuple(notes))


def _canonical_type(raw) -> str:
    """Caller or record property type in HomeHarvest's vocabulary
    (single_family, condos, townhomes, multi_family, mobile, farm, land)."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    return PROPERTY_TYPE_ALIASES.get(s) or scoring.normalize_property_type(s)


def _clean_facts(facts: dict) -> dict:
    out = {}
    for k in FACT_FIELDS:
        v = facts.get(k)
        if v is None or v == "":
            continue
        if k in ("sqft", "lot_sqft", "year_built", "stories"):
            v = int(v)
            if v <= 0:
                raise ArvError("INVALID_INPUT", f"{k} must be a positive number")
        elif k in ("beds", "baths", "garage"):
            v = float(v)
            if v < 0:
                raise ArvError("INVALID_INPUT", f"{k} can't be negative")
        elif k == "property_type":
            v = _canonical_type(v)
        elif k == "has_pool":
            v = bool(v)
        out[k] = v
    return out


# ── Subject resolution ────────────────────────────────────────────────────────

def _cache_key(address: str) -> str:
    return " ".join(address.lower().split())


def _resolve_record(address: str):
    return _record_cache.get_or_set(("record", _cache_key(address)), lambda: hh.resolve_record(address))


def _same_street(a: str, b: str) -> bool:
    base_a, _ = hh.subject_street_key(a)
    base_b, _ = hh.subject_street_key(b)
    return bool(base_a) and base_a == base_b


def _locate(address: str, suggestion: dict | None, latitude, longitude) -> dict | None:
    """Where an unverified address is: {"latitude", "longitude", "source", "matched_address"}."""
    if latitude is not None and longitude is not None:
        return {"latitude": float(latitude), "longitude": float(longitude), "source": "caller",
                "matched_address": None}
    census = census_geocode(address)
    if census:
        return {**census, "source": "census"}
    if suggestion and suggestion.get("area_type") == "address" and _same_street(suggestion.get("text") or "", address):
        c = suggestion.get("centroid") or {}
        if c.get("lat") is not None and c.get("lon") is not None:
            return {"latitude": c["lat"], "longitude": c["lon"], "source": "realtor_building",
                    "matched_address": suggestion.get("text")}
    return None


def _resolve_subject(address: str, facts: dict, latitude, longitude) -> dict:
    listing, suggestion = _resolve_record(address)
    if listing:
        subject = dict(listing)
        if subject.get("property_type"):
            subject["property_type"] = _canonical_type(subject["property_type"])
        subject.update(facts)
        subject["overridden"] = sorted(facts)
        subject["subject_source"] = "record"
        subject["location_source"] = "record"
        if subject.get("latitude") is None or subject.get("longitude") is None:
            c = (suggestion or {}).get("centroid") or {}
            subject["latitude"], subject["longitude"] = c.get("lat"), c.get("lon")
            subject["location_source"] = "realtor_geocoder"
        return subject

    loc = _locate(address, suggestion, latitude, longitude)
    if loc is None:
        raise ArvError(
            "ADDRESS_NOT_FOUND",
            "Couldn't find or locate this address. Ask the user to check the street, number and unit, "
            "or pass latitude/longitude together with the property's facts.",
            address=address,
        )
    missing = [f for f in REQUIRED_FACTS if facts.get(f) in (None, "")]
    if missing:
        raise ArvError(
            "ADDRESS_NOT_VERIFIED",
            "No Realtor.com record matches this exact address, but it was located. Ask the user for the "
            "property's details and call again with them.",
            address=address,
            located={k: loc[k] for k in ("latitude", "longitude", "source", "matched_address")},
            needs=missing,
            recommended=[f for f in RECOMMENDED_FACTS if f not in facts],
        )
    street = address.partition(",")[0]
    _base, unit = hh.subject_street_key(address)
    return {
        "address": street.strip(), "unit": unit, "city": None, "state": None, "zip": None,
        "formatted_address": address,
        **{f: None for f in FACT_FIELDS}, **facts,
        "has_basement": None, "subdivision": None, "special_listing_conditions": [],
        "latitude": loc["latitude"], "longitude": loc["longitude"],
        "mls_status": None, "on_market": False, "listed_price": None,
        "last_sold_price": None, "last_sold_date": None, "property_url": None,
        "address_fuzzy_match": False,
        "overridden": sorted(facts), "subject_source": "user_supplied", "location_source": loc["source"],
        "located_address": loc.get("matched_address"),
    }


# ── Output shaping ────────────────────────────────────────────────────────────

def _known(v):
    """Comps store missing numeric facts as 0; report them as null."""
    return None if v in (None, 0, 0.0, "") else v


def _round(v, n=0):
    if v is None:
        return None
    return round(v) if n == 0 else round(v, n)


def _format_address(subject: dict) -> str:
    if subject.get("formatted_address"):
        return subject["formatted_address"]
    street = " ".join(p for p in (subject.get("address"), subject.get("unit")) if p)
    tail = " ".join(p for p in (subject.get("state"), subject.get("zip")) if p)
    return ", ".join(p for p in (street, subject.get("city"), tail) if p)


def _subject_out(subject: dict) -> dict:
    on_market = bool(subject.get("on_market"))
    return {
        "address": _format_address(subject),
        "subject_source": subject.get("subject_source", "record"),
        "location_source": subject.get("location_source", "record"),
        "located_address": subject.get("located_address"),
        "address_fuzzy_match": bool(subject.get("address_fuzzy_match")),
        "overridden": subject.get("overridden", []),
        "property_type": subject.get("property_type") or None,
        "sqft": _known(subject.get("sqft")),
        "beds": _known(subject.get("beds")),
        "baths": _known(subject.get("baths")),
        "year_built": _known(subject.get("year_built")),
        "lot_sqft": _known(subject.get("lot_sqft")),
        "stories": _known(subject.get("stories")),
        "garage": subject.get("garage"),
        "has_pool": subject.get("has_pool"),
        "has_basement": subject.get("has_basement"),
        "subdivision": subject.get("subdivision"),
        "special_listing_conditions": subject.get("special_listing_conditions") or [],
        "status": subject.get("mls_status"),
        "on_market": on_market,
        "list_price": subject.get("listed_price") if on_market else None,
        "last_sold_price": subject.get("last_sold_price"),
        "last_sold_date": subject.get("last_sold_date"),
        "latitude": subject.get("latitude"),
        "longitude": subject.get("longitude"),
        "property_url": subject.get("property_url"),
    }


def _distress_reasons(comp: dict) -> list[str]:
    reasons = [c for c in comp.get("special_listing_conditions") or []
               if any(w in c.lower() for w in DISTRESS_CONDITION_WORDS)]
    desc = (comp.get("description_text") or "").lower()
    if any(kw in desc for kw in scoring.DISTRESS_KEYWORDS):
        reasons.append("distress keyword in description")
    return reasons


def _factor_notes(breakdown: list[dict]) -> list[str]:
    """The factors that cost this comp points, in the scorer's own words."""
    return [f"{r['factor']}: {r['detail']}" for r in breakdown
            if r["known"] and r["raw"] < 0.999 and r["weight"] > 0]


def _comp_out(v: arv.CompVerdict) -> dict:
    c = v.comp
    reasons = _distress_reasons(c)
    return {
        "address": c.get("address"),
        "sold_price": _known(c.get("sold_price")),
        "price_sqft": _round(c.get("price_sqft")),
        "sold_days_ago": c.get("days_ago"),
        "distance_mi": _round(v.distance_mi, 2),
        "score": _round(v.gated_score, 1),
        "sqft": _known(c.get("sqft")),
        "beds": _known(c.get("beds")),
        "baths": _known(c.get("baths")),
        "year_built": _known(c.get("year_built")),
        "lot_sqft": _known(c.get("lot_sqft")),
        "stories": _known(c.get("stories")),
        "garage": c.get("garage"),
        "has_pool": c.get("has_pool"),
        "subdivision": c.get("subdivision"),
        "special_listing_conditions": c.get("special_listing_conditions") or [],
        "distressed": bool(reasons),
        "distress_reasons": reasons,
        "factors": _factor_notes(v.breakdown),
        "property_url": c.get("property_url"),
    }


def _market_out(subject: dict, listings: list[dict]) -> dict:
    lat, lon = subject.get("latitude"), subject.get("longitude")
    rows = []
    for c in listings:
        dist = None
        if None not in (lat, lon, c.get("latitude"), c.get("longitude")):
            dist = scoring._haversine_miles(lat, lon, c["latitude"], c["longitude"])
        rows.append((dist if dist is not None else float("inf"), dist, c))
    rows.sort(key=lambda t: t[0])
    priced = [c["price_sqft"] for c in listings if c.get("price_sqft")]
    return {
        "note": "Currently listed or under contract nearby. Asking prices, not sales: a ceiling check, "
                "never comps. Not used in the estimate.",
        "active_count": sum(1 for c in listings if c.get("listing_status") == "ACTIVE"),
        "pending_count": sum(1 for c in listings if c.get("listing_status") == "PENDING"),
        "median_list_price_sqft": _round(statistics.median(priced)) if priced else None,
        "listings": [{
            "address": c.get("address"),
            "status": c.get("listing_status"),
            "list_price": _known(c.get("list_price")),
            "price_sqft": _round(c.get("price_sqft")),
            "days_on_market": c.get("days_on_market"),
            "distance_mi": _round(dist, 2),
            "sqft": _known(c.get("sqft")),
            "beds": _known(c.get("beds")),
            "baths": _known(c.get("baths")),
            "year_built": _known(c.get("year_built")),
            "special_listing_conditions": c.get("special_listing_conditions") or [],
        } for _key, dist, c in rows[:MARKET_CONTEXT_CAP]],
    }


def _pool_stats(eligible: list[arv.CompVerdict]) -> dict:
    if not eligible:
        return {"count": 0}
    pps = [v.comp["price_sqft"] for v in eligible]
    clean = [(v.comp["price_sqft"], v.gated_score) for v in eligible if not _distress_reasons(v.comp)]
    med = statistics.median(pps)
    return {
        "count": len(eligible),
        "min_price_sqft": _round(min(pps)),
        "median_price_sqft": _round(med),
        "max_price_sqft": _round(max(pps)),
        "spread_pct": _round((max(pps) - min(pps)) / med * 100, 1) if med else None,
        "distressed_count": len(eligible) - len(clean),
        "weighted_median_price_sqft_excluding_distressed": _round(arv._weighted_median(clean)) if clean else None,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def lookup_property(address: str) -> dict:
    """The verified Realtor.com record for `address`, or ArvError."""
    address = (address or "").strip()
    if not address:
        raise ArvError("INVALID_INPUT", "address is required")
    try:
        listing, suggestion = _resolve_record(address)
    except RateLimited as e:
        raise ArvError("RATE_LIMITED", str(e), retry_after_s=e.retry_after_s) from e
    except UpstreamError as e:
        raise ArvError("UPSTREAM_ERROR", str(e)) from e
    if not listing:
        located = suggestion and suggestion.get("area_type") == "address" and _same_street(
            suggestion.get("text") or "", address)
        raise ArvError(
            "ADDRESS_NOT_VERIFIED" if located else "ADDRESS_NOT_FOUND",
            "No Realtor.com record matches this exact address (street and unit). "
            "Ask the user to check it; get_arv can still estimate from facts they provide.",
            address=address,
            closest_match=suggestion.get("text") if located else None,
        )
    subject = dict(listing, subject_source="record", location_source="record", overridden=[])
    if subject.get("property_type"):
        subject["property_type"] = _canonical_type(subject["property_type"])
    return {"subject": _subject_out(subject)}


def get_arv(address: str, *, sqft=None, beds=None, baths=None, year_built=None, lot_sqft=None,
            property_type=None, stories=None, garage=None, has_pool=None, latitude=None, longitude=None,
            radius_mi=None, days_back=None, include_market_context: bool = True,
            max_comps: int | None = None) -> dict:
    address = (address or "").strip()
    if not address:
        raise ArvError("INVALID_INPUT", "address is required")
    if (latitude is None) != (longitude is None):
        raise ArvError("INVALID_INPUT", "latitude and longitude must be given together")
    try:
        return _get_arv(address, _clean_facts(dict(
            sqft=sqft, beds=beds, baths=baths, year_built=year_built, lot_sqft=lot_sqft,
            property_type=property_type, stories=stories, garage=garage, has_pool=has_pool)),
            latitude, longitude, _window(radius_mi, days_back), include_market_context,
            min(MAX_COMPS_CAP, max(1, int(max_comps or DEFAULT_MAX_COMPS))))
    except RateLimited as e:
        raise ArvError("RATE_LIMITED", str(e), retry_after_s=e.retry_after_s) from e
    except UpstreamError as e:
        raise ArvError("UPSTREAM_ERROR", str(e)) from e
    except (TypeError, ValueError) as e:
        raise ArvError("INVALID_INPUT", f"Invalid input: {e}") from e


def _fetch(subject: dict, window: Window, include_market_context: bool):
    """(sold comps, active/pending listings) around the subject, cached per
    location + window + the filters the fetch itself uses."""
    key = (round(subject["latitude"], 5), round(subject["longitude"], 5), window.radius_mi, window.days_back,
           subject.get("property_type") or "", hh._sqft_band(subject.get("sqft")))
    with ThreadPoolExecutor(max_workers=2) as pool:
        sold_f = pool.submit(_comp_cache.get_or_set, ("sold",) + key,
                             lambda: fetch_comps(subject, window.radius_mi, window.days_back))
        market_f = pool.submit(_comp_cache.get_or_set, ("market",) + key,
                               lambda: fetch_active_pending_comps(subject, window.radius_mi)) \
            if include_market_context else None
        sold = sold_f.result()
        market = market_f.result() if market_f else None
    return list(sold), (list(market) if market is not None else None)


def _get_arv(address, facts, latitude, longitude, window: Window, include_market_context, max_comps) -> dict:
    subject = _resolve_subject(address, facts, latitude, longitude)
    guidance: list[str] = list(window.adjusted)

    ptype = scoring.normalize_property_type(subject.get("property_type"))
    if ptype == "land":
        raise ArvError("UNSUPPORTED_PROPERTY_TYPE",
                       "Vacant land has no living area to price per sqft, so a comp-based $/sqft ARV doesn't apply.",
                       subject=_subject_out(subject))
    if ptype in PROPERTY_TYPE_WARNINGS:
        guidance.append(PROPERTY_TYPE_WARNINGS[ptype])
    if not ptype:
        guidance.append("Property type is unknown, so comps of every type were searched; ask the user to confirm it.")
    if not subject.get("sqft"):
        raise ArvError("MISSING_FACTS", "The record has no living-area sqft, which the estimate needs. "
                       "Ask the user for it and call again with sqft.",
                       subject=_subject_out(subject), needs=["sqft"])
    if subject.get("latitude") is None or subject.get("longitude") is None:
        raise ArvError("ADDRESS_NOT_FOUND", "The property was found but has no map location; "
                       "pass latitude/longitude to estimate it.", subject=_subject_out(subject))

    sold, market = _fetch(subject, window, include_market_context)
    comps, own_sales = split_subject(sold, _subject_street_key(subject))
    if not comps:
        raise ArvError("NO_COMPS", f"No sold comps found within {window.radius_mi:g} mi in the last "
                       f"{window.days_back} days. Try a larger radius_mi or days_back.",
                       subject=_subject_out(subject))

    half_life = scoring.recency_half_life(window.days_back)
    win = dict(max_distance_mi=window.radius_mi, recency_half_life_days=half_life)
    scored = scoring.score_all_comps_gated(subject, comps, half_life)
    classified = arv.classify_comps(subject, comps, scored=scored, **win)
    est = arv.estimate_arv_gated(subject, comps, classified=classified, **win)
    if est is None:
        raise ArvError("NO_COMPS", "Comps were found, but none had a usable location or price to anchor an "
                       "estimate.", subject=_subject_out(subject))
    eligible = [v for v in classified if v.eligible]
    excluded = [v for v in classified if not v.eligible]
    fallback = not est["eligible"]
    tier = arv.comp_confidence_tier(eligible)
    renovated = None if fallback else arv.renovated_range(eligible, subject["sqft"])
    stats = _pool_stats(eligible)

    # Guidance, most important first.
    if fallback:
        guidance.insert(0, "FALLBACK ESTIMATE: no comp passed every check, so this is the area's typical "
                           "$/sqft, not comp-backed. Say so plainly and lean on the excluded comps' reasons.")
    if subject["subject_source"] == "user_supplied":
        guidance.insert(0, f"Subject is user-supplied and unverified (located via {subject['location_source']}); "
                           "the estimate is only as good as the facts provided.")
    if subject.get("address_fuzzy_match"):
        guidance.append(f"Address was fuzzy-matched to {_format_address(subject)}; confirm it's the right property.")
    if subject.get("location_source") == "realtor_geocoder":
        guidance.append("The record had no coordinates; distances use Realtor.com's geocoded point.")
    if 0 < len(eligible) < 3:
        guidance.append(f"Only {len(eligible)} eligible comp(s); treat the estimate as low confidence.")
    if renovated is None and not fallback and eligible:
        guidance.append(f"Too few eligible comps (< {arv.RENOVATED_MIN_COMPS}) to separate renovated sales "
                        "from typical ones; no renovated range.")
    if stats.get("distressed_count"):
        share = stats["distressed_count"] / stats["count"]
        clean_med = stats.get("weighted_median_price_sqft_excluding_distressed")
        gap = abs(clean_med / est["price_sqft"] - 1) if clean_med and est["price_sqft"] else 0
        if share >= DISTRESSED_SHARE_NOTE or gap >= DISTRESSED_MEDIAN_GAP_NOTE:
            guidance.append(f"{stats['distressed_count']} of {stats['count']} eligible comps are distressed sales "
                            f"(REO/short sale/fixer); without them the median is ${clean_med}/sqft.")
    conditions = [c for c in subject.get("special_listing_conditions") or []
                  if any(w in c.lower() for w in DISTRESS_CONDITION_WORDS)]
    if conditions:
        guidance.append(f"Subject itself is listed as {', '.join(conditions)}.")
    recent_own = None
    if own_sales:
        own = min(own_sales, key=lambda c: c.get("days_ago") or 0)
        recent_own = {"sold_price": _known(own.get("sold_price")), "price_sqft": _round(own.get("price_sqft")),
                      "sold_days_ago": own.get("days_ago")}
        guidance.append(f"The subject itself sold {own.get('days_ago')} days ago for "
                        f"${own.get('sold_price') or 0:,.0f}; that sale is strong direct evidence (excluded "
                        "from its own comps). Compare it with the estimate.")
    if not window.is_default:
        guidance.append(f"Searched {window.radius_mi:g} mi / {window.days_back} days instead of the default "
                        f"{hh.COMP_RADIUS_MILES:g} mi / {hh.COMP_DAYS} days.")

    subject_out = _subject_out(subject)
    if recent_own:
        subject_out["recent_sale_in_window"] = recent_own

    result = {
        "subject": subject_out,
        "estimate": {
            "arv": est["arv"],
            "price_sqft": _round(est["price_sqft"]),
            "basis": "typical value: similarity-weighted median $/sqft of eligible sold comps x subject sqft",
            "renovated_range": None if renovated is None else {
                "low": renovated["low"], "high": renovated["high"],
                "low_price_sqft": _round(renovated["low_price_sqft"]),
                "high_price_sqft": _round(renovated["high_price_sqft"]),
                "basis": "weighted 75th-90th percentile $/sqft of eligible comps",
            },
            "method": "area_anchor_fallback" if fallback else "weighted_median",
            "arv_fallback": fallback,
            "window": {"radius_mi": window.radius_mi, "days_back": window.days_back},
        },
        "confidence": {"tier": tier, "explanation": CONFIDENCE_EXPLANATIONS[tier]},
        "pool_stats": stats,
        "eligible_comps": [_comp_out(v) for v in eligible[:max_comps]],
        "excluded_comps": [{
            "address": v.comp.get("address"),
            "price_sqft": _round(v.comp.get("price_sqft")),
            "distance_mi": _round(v.distance_mi, 2),
            "reason": v.exclusion_reason,
        } for v in excluded[:EXCLUDED_CAP]],
        "excluded_count": len(excluded),
        "guidance": guidance,
    }
    if market is not None:
        result["market_context"] = _market_out(subject, market)
    return result
