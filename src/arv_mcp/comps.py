"""
comps.py — Fetches sold comps and nearby active/pending listings from
HomeHarvest (Realtor.com) and normalizes them to the internal comp format.
"""

import logging
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime

from .data.homeharvest import (
    COMP_DAYS,
    COMP_RADIUS_MILES,
    _normalize_street,
    _normalize_street_suffix,
    _normalize_unit,
    _split_unit,
    extract_garage,
    extract_pool_basement,
    extract_special_conditions,
    extract_stories,
    extract_subdivision,
    get_active_pending,
    get_comps,
)

log = logging.getLogger(__name__)


def build_location(listing: dict) -> str:
    """Build a location string from listing fields."""
    parts = [
        listing.get("address", ""),
        listing.get("city", ""),
        listing.get("state", ""),
        listing.get("zip", ""),
    ]
    return ", ".join(p for p in parts if p).strip()

# Fetch radius/date-window/sqft-tolerance/candidate-limit all live in
# data/homeharvest.py's get_comps(). This module only drops candidates
# that are outright bad data (_drop_bulk_sale_artifacts,
# _drop_price_sqft_outliers) — real comparability filtering happens in
# scoring.py's gated scoring, on whatever's left.

# Maps common human-readable property-type labels ("Single Family", "Condo",
# "Townhouse", ...) to HomeHarvest property_type values, so a caller can pass
# either vocabulary.
PROPERTY_TYPE_ALIASES = {
    "single family": "single_family",
    "single family (residential)": "single_family",
    "multi-family (2-4 units)":  "multi_family",
    "multi family":              "multi_family",
    "condominium":               "condos",
    "condominium (residential)":   "condos",
    "condo":                     "condos",
    "condo (residential)":         "condos",
    "townhome":                  "townhomes",
    "townhome (residential)":      "townhomes",
    "townhouse":                 "townhomes",
    "townhouse (residential)":     "townhomes",
    "duplex":                    "duplex_triplex",
    "triplex":                   "duplex_triplex",
    "duplex/triplex":            "duplex_triplex",
    "mobile/manufactured":       "mobile",
    "mobile home":               "mobile",
    "farm":                      "farm",
    "land":                      "land",
    "vacant land":               "land",
}


def fetch_comps(listing: dict, radius_mi: float = COMP_RADIUS_MILES, days_back: int = COMP_DAYS) -> list:
    """
    Sold comps around the listing's own lat/lon, normalized to comp dicts.

    The subject itself is NOT removed here — use split_subject(), which also
    hands back the subject's own sale so it can be reported rather than
    silently counted as a comp at 0 mi. Upstream failures propagate
    (client.UpstreamError / RateLimited) instead of reading as "no comps".
    Empty list when the listing has no coordinates.
    """
    lat, lon = listing.get("latitude"), listing.get("longitude")
    if lat is None or lon is None:
        log.warning("Cannot fetch comps — listing has no coordinates.")
        return []

    property_types = _resolve_property_types(listing)
    results = get_comps(lat, lon, property_types, listing.get("sqft"), radius_mi, days_back)
    if not results:
        log.debug(f"No comps found near {build_location(listing)}")
        return []

    normalized = _normalize_comps(results)
    type_mix = Counter(c["type"] or "(blank)" for c in normalized)
    log.info(f"Comp type mix for {build_location(listing)} (requested={property_types}): {dict(type_mix)}")
    return normalized


def fetch_active_pending_comps(listing: dict, radius_mi: float = COMP_RADIUS_MILES) -> list:
    """
    Active-for-sale and pending listings around the listing's lat/lon —
    market context only. ARV is computed from fetch_comps() above; nothing in
    this function's return value is ever fed into ARV.

    Normalized to the same comp-dict shape sold comps use (minus sold_price,
    plus list_price/listing_status), with the subject's own listing dropped.
    Empty list when the listing has no coordinates.
    """
    lat, lon = listing.get("latitude"), listing.get("longitude")
    if lat is None or lon is None:
        return []

    property_types = _resolve_property_types(listing)
    results = get_active_pending(lat, lon, property_types, listing.get("sqft"), radius_mi)
    if not results:
        return []

    normalized = _normalize_active_pending(results, _subject_street_key(listing))
    log.info(f"Fetched {len(normalized)} active/pending listings for {build_location(listing)}")
    return normalized


def split_subject(comps: list[dict], subject_key: tuple[str, str | None]) -> tuple[list[dict], list[dict]]:
    """(others, subject_sales): comps whose street (and unit, when the subject
    names one) match subject_key are the subject's own sales — a recently
    sold subject shows up in its own sold-comp search at 0 mi."""
    subject_base, subject_unit = subject_key
    if not subject_base:
        return comps, []
    others, own = [], []
    for c in comps:
        base, unit = c.get("_street_key") or ("", None)
        if base == subject_base and (subject_unit is None or unit == subject_unit):
            own.append(c)
        else:
            others.append(c)
    return others, own


def _subject_street_key(listing: dict) -> tuple[str, str | None]:
    """Normalized (base_street, unit) for the subject listing's own address —
    used to filter the subject's own listing out of its active/pending
    results. The subject is almost always itself for sale, and
    the HomeHarvest search radius is centered on its own address, so without
    this it would very likely comp itself against itself. Reuses
    homeharvest.py's own address-matching helpers — the same
    normalize-then-split-unit comparison match_property uses. A listing's
    separate `unit` field counts when the street text has none."""
    addr = (listing.get("address") or "").strip()
    if not addr:
        return "", None
    base, unit = _split_unit(_normalize_street(_normalize_street_suffix(addr)))
    return base, unit or _normalize_unit(listing.get("unit"))


# Raw HomeHarvest `status` values that mean "under contract, not yet closed" —
# grouped under PENDING for comping purposes (CONTINGENT still means the
# seller is taking backup offers, same practical status as PENDING here).
_PENDING_STATUSES = {"PENDING", "CONTINGENT"}


def _listing_status(raw_status) -> str:
    """Collapse a HomeHarvest status value to ACTIVE or PENDING for display."""
    s = str(raw_status or "").upper()
    return "PENDING" if s in _PENDING_STATUSES else "ACTIVE"


def _normalize_active_pending(results, subject_key: tuple[str, str | None] = ("", None)) -> list:
    """Normalize HomeHarvest active/pending Property objects to the internal
    comp format — same shape _normalize_comps produces for sold comps, minus
    sold_price/_building_key (no sale happened, so bulk-sale-artifact
    detection doesn't apply) plus list_price/listing_status.

    subject_key (see _subject_street_key) drops any result that IS the
    subject's own listing — a same-address/radius HomeHarvest search will
    otherwise very likely return the subject itself, since it's almost
    always for sale too."""
    normalized = []
    for p in results:
        addr = p.address
        desc = p.description

        street = (addr.street or "") if addr else ""
        unit   = (addr.unit or "") if addr else ""
        city   = (addr.city or "") if addr else ""
        state  = (addr.state or "") if addr else ""

        # Same base-street + (no-unit-required-or-matching-unit) comparison
        # homeharvest._find_matching_property already uses to decide
        # "is this HomeHarvest result the same property as my target address"
        # — applied here in the other direction, against the subject's own
        # (possibly unit-baked-into-street) address.
        subject_base, subject_unit = subject_key
        if street and subject_base:
            result_street = _normalize_street(street)
            result_unit = _normalize_unit(unit)
            if result_street == subject_base and (subject_unit is None or result_unit == subject_unit):
                log.info(f"Dropped '{street}' from active/pending results — matches the subject's own address")
                continue

        street_line = f"{street} {unit}".strip() if unit else street
        address = ", ".join(part for part in [street_line, city, state] if part)

        baths = 0.0
        if desc:
            baths = (desc.baths_full or 0) + (desc.baths_half or 0) * 0.5

        has_pool, has_basement = extract_pool_basement(p.tags)

        status = _listing_status(p.status)
        sqft = (desc.sqft if desc else None) or 0
        list_price = p.list_price or 0
        # prc_sqft is HomeHarvest's own $/sqft when present; falls back to a
        # manual list_price/sqft calc (mirrors _normalize_comps' price_sqft,
        # which relies on prc_sqft too — see comps.py module docstring).
        price_sqft = float(p.prc_sqft) if p.prc_sqft is not None else (
            (list_price / sqft) if list_price and sqft else 0.0
        )
        # Pending's freshness signal is when it went pending, not when it was
        # first listed; active listings only have a list date.
        date_field = p.pending_date if (status == "PENDING" and p.pending_date) else p.list_date

        normalized.append({
            "address":    address,
            "city":       city,
            "state":      state,
            "listing_status": status,
            "list_price": list_price,
            "sold_price": None,
            "type":       (desc.style.value if desc and desc.style else "") or "",
            "baths":      baths,
            "year_built": (desc.year_built if desc else None) or 0,
            "sqft":       sqft,
            "beds":       (desc.beds if desc else None) or 0,
            "days_ago":   _days_ago(date_field),
            "days_on_market": p.days_on_mls,
            "price_sqft": price_sqft,
            "lot_sqft":   (desc.lot_sqft if desc else None) or 0,
            "garage":     extract_garage(p),
            "stories":    extract_stories(p) or 0,
            "latitude":   p.latitude,
            "longitude":  p.longitude,
            "subdivision":       extract_subdivision(p),
            "description_text":  (desc.text if desc else None) or "",
            "has_pool":          has_pool,
            "has_basement":      has_basement,
            "special_listing_conditions": extract_special_conditions(p),
            "property_url":      str(p.property_url) if p.property_url else None,
        })
    return _drop_price_sqft_outliers(normalized)


def _resolve_property_types(listing: dict) -> list | None:
    """
    Return the property_type list to pass to HomeHarvest.
    Maps the listing's own property_type via PROPERTY_TYPE_ALIASES if it's a
    human-readable label; if it's already a HomeHarvest type (e.g. when the
    listing was built by homeharvest.get_full_listing()), it's used as-is.
    Returns None if no match is found (HomeHarvest will return all types).
    """

    raw = (listing.get("property_type") or "").strip().lower()
    mapped = PROPERTY_TYPE_ALIASES.get(raw)
    if mapped:
        return [mapped]
    if raw in PROPERTY_TYPE_ALIASES.values():
        return [raw]

    log.debug(f"No HomeHarvest property_type mapping for '{raw}' — fetching all types")
    return None

def _days_ago(sold_date) -> int:
    """Calculate how many days ago a property sold from a date/string value."""
    if sold_date is None:
        return 0
    try:
        if isinstance(sold_date, str):
            sold_date = datetime.strptime(sold_date[:10], "%Y-%m-%d").date()
        elif hasattr(sold_date, "date"):
            sold_date = sold_date.date()
        return (date.today() - sold_date).days
    except Exception:
        return 0


def _normalize_comps(results) -> list:
    """Normalize a list of HomeHarvest pydantic Property objects to the internal comp format."""
    normalized = []
    for p in results:
        addr = p.address
        desc = p.description

        street = (addr.street or "") if addr else ""
        unit   = (addr.unit or "") if addr else ""
        city   = (addr.city or "") if addr else ""
        state  = (addr.state or "") if addr else ""
        # Unit included in the display address (but not in _building_key below)
        # so distinct condo/townhome units in the same building don't render
        # as identical addresses — see _drop_bulk_sale_artifacts.
        street_line = f"{street} {unit}".strip() if unit else street
        address = ", ".join(part for part in [street_line, city, state] if part)
        building_key = ", ".join(part for part in [street, city, state] if part)

        baths = 0.0
        if desc:
            baths = (desc.baths_full or 0) + (desc.baths_half or 0) * 0.5

        has_pool, has_basement = extract_pool_basement(p.tags)

        normalized.append({
            "address":    address,
            "_building_key": building_key,
            # (base street, unit) in subject_street_key form, for split_subject.
            "_street_key": (_normalize_street(street), _normalize_unit(unit)),
            "city":       city,
            "state":      state,
            # description.sold_price is the primary source; Property.last_sold_price
            # is a fallback for the (rarer) case HomeHarvest populates one but not
            # the other. Doesn't feed ARV math (that's price_sqft-driven) — this
            # only affects the sold_price reported for the comp.
            "sold_price": (desc.sold_price if desc else None) or p.last_sold_price or 0,
            "type":       (desc.style.value if desc and desc.style else "") or "",
            "baths":      baths,
            "year_built": (desc.year_built if desc else None) or 0,
            "sqft":       (desc.sqft if desc else None) or 0,
            "beds":       (desc.beds if desc else None) or 0,
            "days_ago":   _days_ago(p.last_sold_date),
            # Not coerced to 0 like the fields below — None here means the MLS
            # didn't report it, as distinct from a genuine same-day sale (0
            # days on market).
            "days_on_market": p.days_on_mls,
            "price_sqft": float(p.prc_sqft) if p.prc_sqft is not None else 0.0,
            "lot_sqft":   (desc.lot_sqft if desc else None) or 0,
            "garage":     extract_garage(p),
            "stories":    extract_stories(p) or 0,
            "latitude":   p.latitude,
            "longitude":  p.longitude,
            "subdivision":       extract_subdivision(p),
            "description_text":  (desc.text if desc else None) or "",
            "has_pool":          has_pool,
            "has_basement":      has_basement,
            "special_listing_conditions": extract_special_conditions(p),
            "property_url":      str(p.property_url) if p.property_url else None,
        })
    return _drop_price_sqft_outliers(_drop_bulk_sale_artifacts(normalized))


# A comp's price_sqft this many multiples off its own candidate pool's
# median indicates a data error (a bad MLS/sold-price record, or a
# bulk/portfolio sale price _drop_bulk_sale_artifacts' exact-duplicate
# signature didn't catch because it only occurred once in this pool) rather
# than a genuinely comparable sale. Dropped outright rather than scored
# down — scoring.py has no factor that would zero out a comp's
# influence on ARV, so even a comp that clears every gate and the
# micro-market check (see arv.classify_comps) would still let a
# garbage price distort the $/sqft-weighted median.
PRICE_SQFT_OUTLIER_MULTIPLE = 5


def _drop_price_sqft_outliers(comps: list[dict]) -> list[dict]:
    """Drop comps whose price_sqft is more than PRICE_SQFT_OUTLIER_MULTIPLE
    away (in either direction) from this pool's own median price_sqft.
    Skipped entirely for pools too small for a median to mean anything."""
    priced = [c["price_sqft"] for c in comps if c.get("price_sqft")]
    if len(priced) < 5:
        return comps

    median = statistics.median(priced)
    if median <= 0:
        return comps

    lo, hi = median / PRICE_SQFT_OUTLIER_MULTIPLE, median * PRICE_SQFT_OUTLIER_MULTIPLE
    kept = []
    for c in comps:
        ps = c.get("price_sqft") or 0
        if ps and not (lo <= ps <= hi):
            log.info(
                f"Dropped comp '{c.get('address')}' — price_sqft ${ps:,.0f} is "
                f">{PRICE_SQFT_OUTLIER_MULTIPLE}x off this pool's median ${median:,.0f}, "
                f"likely a bad MLS/sold-price record"
            )
            continue
        kept.append(c)
    return kept


def _drop_bulk_sale_artifacts(comps: list[dict]) -> list[dict]:
    """
    Drop comps sharing an identical non-zero sold_price with another unit at the
    same building (_building_key) — HomeHarvest/Realtor.com sometimes attributes
    one whole-building/portfolio sale price to every individual unit's MLS
    record, which turns that shared total into a wildly inflated per-unit
    price_sqft (e.g. a $7.15M "sold_price" on a single 1,400 sqft condo unit).
    Two or more genuinely separate unit sales landing on the exact same dollar
    figure is not a realistic coincidence, so this is a reliable signature for
    the artifact — unlike a price/sqft threshold, which would also catch
    legitimate high-end sales.
    """
    groups = defaultdict(list)
    for c in comps:
        groups[(c["_building_key"], c["sold_price"])].append(c)

    dropped = 0
    kept = []
    for (building_key, sold_price), group in groups.items():
        if sold_price and len(group) >= 2:
            dropped += len(group)
            log.info(
                f"Dropped {len(group)} comps at '{building_key}' sharing identical "
                f"sold_price ${sold_price:,.0f} — likely a bulk/portfolio sale "
                f"price misattributed to each unit"
            )
            continue
        kept.extend(group)

    for c in kept:
        del c["_building_key"]
    return kept
