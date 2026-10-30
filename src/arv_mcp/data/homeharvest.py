"""
homeharvest.py — HomeHarvest (Realtor.com) data layer: verified subject
lookup, sold comps and nearby active/pending listings, plus the extractors
that turn a HomeHarvest Property into the fields scoring.py uses.

All network access goes through data/client.py (timeouts, rate limiting,
coordinate-centered searches). Every search here is centered on a lat/lon,
never on re-geocoded address text.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from homeharvest.core.scrapers.models import ListingType

from . import client

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
# These deliberately cast a wide net — arv.py's classify_comps() does
# the actual filtering (per-factor gates plus the micro-market $/sqft check),
# so get_comps() should hand it as large a candidate pool as HomeHarvest can
# return rather than pre-filtering comps the classifier would still credit.
COMP_RADIUS_MILES = 1.5
COMP_DAYS         = 180
COMP_SQFT_TOL     = 40    # wider than scoring.GATE_SQFT_ZERO_PCT (30%) — no point pre-filtering
                           # tighter than what the scorer already zeroes out

# Raw candidate ceiling fetched from HomeHarvest, sorted sold_date desc —
# scoring.py scores and filters whatever this hands it. In high-turnover
# zips the true sqft/radius/date-matched pool can run into the hundreds, so
# this needs real headroom or good recent comps get silently cut off before
# scoring ever sees them.
COMP_FETCH_LIMIT  = 500

# Active/pending are market context only; the nearest few dozen are plenty.
ACTIVE_PENDING_LIMIT = 200


# HomeHarvest's `Property.tags` values for a private in-ground pool and a
# basement — confirmed against live data. "community_swimming_pool" is
# deliberately excluded: it's a shared amenity, not a private pool on the
# unit's own lot, so it isn't comparable the same way for ARV purposes.
POOL_TAG     = "swimming_pool"
BASEMENT_TAG = "basement"


def extract_pool_basement(tags: list[str] | None) -> tuple[bool, bool]:
    """(has_pool, has_basement) from a Property's `tags` list."""
    tags = tags or []
    return POOL_TAG in tags, BASEMENT_TAG in tags


def _detail_value(prop, key: str) -> str | None:
    """Value of the first "Key: value" line in a Property's MLS `details`
    text (case-insensitive key match), or None if no such line. `details`
    entries are HomeDetails models from a live fetch but plain dicts in some
    callers/tests, so both shapes are read."""
    prefix = key.lower() + ":"
    for detail in (getattr(prop, "details", None) or []):
        texts = (detail.get("text") or []) if isinstance(detail, dict) else (getattr(detail, "text", None) or [])
        for text in texts:
            if isinstance(text, str) and text.lower().startswith(prefix):
                return text.split(":", 1)[1].strip()
    return None


# Realtor.com's `description.stories` is the MLS "Building Total Stories"
# field — the height of the whole building, not the unit. For a condo or
# townhome that's wrong for comping (a single-level unit in a 4-story
# building reports 4). The MLS "Levels" line in `details` is the unit's own
# level count, so it's read first; description.stories is only a fallback
# for MLSs that don't send a Levels line. Confirmed against live ARMLS data.
_LEVEL_WORDS = {"one": 1, "single": 1, "two": 2, "three": 3, "four": 4}


def extract_stories(prop) -> int | None:
    """Unit level count: MLS "Levels" line first, description.stories (the
    building's total stories) as fallback. None when neither is usable.

    A unit can't have more levels than its building has stories, so Levels is
    capped at the building total when both are present — catches agent typos
    like "Levels: 14" on a 1-story single-family home."""
    desc = getattr(prop, "description", None)
    building = getattr(desc, "stories", None) or None
    levels = _detail_value(prop, "Levels")
    if levels:
        match = re.search(r"\d+", levels)
        n = int(match.group()) if match else _LEVEL_WORDS.get(levels.split()[0].lower())
        if n:
            return min(n, building) if building else n
    return building


# HomeHarvest's garage-count tags, highest first — a lower bound on spaces
# when the listing gives no explicit count.
_GARAGE_TAG_COUNTS = (("garage_3_or_more", 3.0), ("garage_2_or_more", 2.0), ("garage_1_or_more", 1.0))


def extract_garage(prop) -> float | None:
    """Garage spaces: MLS "Garage Spaces" line, then description.garage, then
    the garage_N_or_more tags as a lower bound. description.garage comes back
    blank for carport-only and no-garage homes, so a listing that has tags
    but no garage tag is a real 0 rather than unknown. None only when the
    listing has no count and no tags at all to judge by."""
    spaces = _detail_value(prop, "Garage Spaces")
    if spaces:
        try:
            return float(spaces)
        except ValueError:
            pass
    desc = getattr(prop, "description", None)
    garage = getattr(desc, "garage", None)
    if garage is not None:
        return float(garage)
    tags = getattr(prop, "tags", None)
    if not tags:
        return None
    for tag, count in _GARAGE_TAG_COUNTS:
        if tag in tags:
            return count
    return 0.0


def extract_special_conditions(prop) -> list[str]:
    """MLS "Special Listing Conditions" values (e.g. "Lender Owned/REO",
    "Pre-Foreclosure", "Short Sale", "Auction", "Probate Listing",
    "Owner/Agent"), with "N/A" dropped. HomeHarvest's own
    flags.is_foreclosure is never populated — its GraphQL query doesn't
    request that field — so this details line is the only foreclosure/REO
    signal available. Empty list when the line is absent or only "N/A"."""
    raw = _detail_value(prop, "Special Listing Conditions")
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip() and c.strip().upper() != "N/A"]


# Placeholder values MLSs put in the Subdivision line instead of leaving it blank.
_NO_SUBDIVISION = {"N/A", "NA", "NONE", "UNKNOWN", "OTHER", "NOT IN SUBDIVISION", "NO SUBDIVISION",
                   "METES AND BOUNDS", "METES & BOUNDS"}


def extract_subdivision(prop) -> str | None:
    """MLS "Subdivision" line, as written (e.g. "KNOELL MESA UNIT 2"), or
    None when absent or a placeholder. Property.neighborhoods looks like the
    natural source but came back empty on every sold comp in live probes
    across six markets; this details line was populated on ~67-87% of them.
    The "Source Neighborhood" line duplicates it when present and otherwise
    falls back to vague regions ("West"), so it isn't used.
    scoring.subdivision_key() normalizes this for matching."""
    raw = _detail_value(prop, "Subdivision")
    if not raw or raw.strip().upper() in _NO_SUBDIVISION:
        return None
    return raw.strip()

# ── Comp searches (centered on the subject's coordinates) ─────────────────────

def _sqft_band(sqft: int | None) -> tuple[int | None, int | None]:
    if not sqft:
        return None, None
    tol = sqft * COMP_SQFT_TOL / 100
    return int(sqft - tol), int(sqft + tol)


def get_comps(lat: float, lon: float, property_types: list | None, sqft: int | None,
              radius_mi: float = COMP_RADIUS_MILES, days_back: int = COMP_DAYS) -> list:
    """
    Sold comps within `radius_mi` of (lat, lon) that sold in the last
    `days_back` days, newest first, as pydantic Property objects (which carry
    `tags` and MLS `details`, where pool/stories/garage/subdivision live).
    Raises client.UpstreamError / RateLimited rather than returning [], so a
    blocked request is never mistaken for "no sales nearby".
    """
    sqft_min, sqft_max = _sqft_band(sqft)
    results = client.search_near(
        lat, lon, listing_type=ListingType.SOLD, radius_mi=radius_mi, property_types=property_types,
        sqft_min=sqft_min, sqft_max=sqft_max, past_days=days_back, sort_by="sold_date",
        limit=COMP_FETCH_LIMIT,
    )
    log.debug(f"Fetched {len(results)} raw sold comps around ({lat:.5f}, {lon:.5f})")
    return results


def get_active_pending(lat: float, lon: float, property_types: list | None, sqft: int | None,
                       radius_mi: float = COMP_RADIUS_MILES) -> list:
    """
    Active-for-sale and pending/contingent listings near (lat, lon) — market
    context only, never fed into ARV.

    Two requests run IN PARALLEL rather than one combined
    listing_type=["for_sale", "pending"] call — confirmed empirically that the
    combined form silently drops every plain FOR_SALE result and returns only
    PENDING/CONTINGENT ones. "for_sale" (with exclude_pending=True, so it
    can't re-include what the pending call covers) and "pending" separately
    return the full set. Each result keeps its own `status`.
    """
    sqft_min, sqft_max = _sqft_band(sqft)
    common = dict(radius_mi=radius_mi, property_types=property_types, sqft_min=sqft_min,
                  sqft_max=sqft_max, limit=ACTIVE_PENDING_LIMIT)
    with ThreadPoolExecutor(max_workers=2) as executor:
        active_future = executor.submit(client.search_near, lat, lon, listing_type=ListingType.FOR_SALE,
                                        exclude_pending=True, **common)
        pending_future = executor.submit(client.search_near, lat, lon, listing_type=ListingType.PENDING,
                                         **common)
        active, pending = active_future.result(), pending_future.result()
    log.debug(f"Fetched {len(active)} active, {len(pending)} pending around ({lat:.5f}, {lon:.5f})")
    return active + pending

ACTIVE_STATUSES = {"ACTIVE", "FOR_SALE"}
INACTIVE_STATUSES = {"CONTINGENT", "PENDING", "SOLD", "OFF_MARKET", "CLOSED", "EXPIRED", "WITHDRAWN", "CANCELLED"}


def _normalize_street(value: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace for street comparison."""
    value = value.lower()
    value = re.sub(r"[^\w\s]", "", value)
    return re.sub(r"\s+", " ", value).strip()


# Common USPS street-suffix abbreviations. Realtor.com's free-text geocoder
# can anchor to a noticeably different point (500+ ft) for the spelled-out
# form ("...85th Street") vs the abbreviated form ("...85th St") — enough to
# push the actual house outside the tight radius resolve_record
# uses for single-property lookups. Abbreviating before querying makes the
# geocode land where HomeHarvest's own data expects it.
_STREET_SUFFIX_MAP = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr",
    "court": "ct", "lane": "ln", "road": "rd", "place": "pl",
    "circle": "cir", "terrace": "ter", "trail": "trl", "parkway": "pkwy",
    "highway": "hwy", "square": "sq", "crescent": "cres", "alley": "aly",
    "point": "pt", "ridge": "rdg", "crossing": "xing", "close": "cl",
    "cove": "cv", "estates": "ests", "expressway": "expy",
    "extension": "ext", "freeway": "fwy", "gardens": "gdns", "grove": "grv",
    "harbor": "hbr", "heights": "hts", "hollow": "holw", "island": "is",
    "junction": "jct", "landing": "lndg", "manor": "mnr", "meadows": "mdws",
    "mills": "mls", "mount": "mt", "mountain": "mtn", "orchard": "orch",
    "pike": "pike", "pines": "pnes", "plaza": "plz", "port": "prt",
    "ranch": "rnch", "shore": "shr", "shores": "shrs", "spring": "spg",
    "springs": "spgs", "station": "sta", "summit": "smt", "trace": "trce",
    "tunnel": "tunl", "turnpike": "tpke", "valley": "vly", "view": "vw",
    "village": "vlg", "vista": "vis", "wells": "wls",
}


_STREET_SUFFIX_WORDS = set(_STREET_SUFFIX_MAP.keys()) | set(_STREET_SUFFIX_MAP.values())
_UNIT_KEYWORDS = {"unit", "apt", "apartment", "ste", "suite", "lot", "spc", "space"}
# A lone trailing directional after the suffix (e.g. "...Dr N") is a street
# continuation, not a unit -- USPS post-directionals, not condo/townhome units.
_DIRECTIONAL_TOKENS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}


def _split_unit(normalized_street: str) -> tuple[str, str | None]:
    """
    Split an already-_normalize_street'd street+unit string into (base_street,
    unit). MLS "Short Address" exports bake the unit straight onto the street
    text (e.g. "400 tower st 601") with no separating keyword, and HomeHarvest
    only ever puts the base street in Address.street (unit is its own field) —
    so a query like that can never exact-match any candidate's street, and
    used to fall through to the fuzzy startswith check, which matched ANY unit
    in the building.

    Finds the street-type word (st, ave, dr, ...) and treats everything after
    it as the unit, dropping a leading unit keyword (unit/apt/ste/suite) if
    present. Returns (normalized_street, None) — leaving the string intact so
    matching falls back to whole-string comparison — when no recognized
    suffix word is found, or when what follows it is just a lone post-
    directional ("...Dr N") rather than a unit.
    """
    words = normalized_street.split()
    suffix_idx = None
    for i, word in enumerate(words):
        if word in _STREET_SUFFIX_WORDS:
            suffix_idx = i
    if suffix_idx is None or suffix_idx == len(words) - 1:
        return normalized_street, None

    base = " ".join(words[: suffix_idx + 1])
    unit_tokens = words[suffix_idx + 1 :]
    if unit_tokens and unit_tokens[0] in _UNIT_KEYWORDS:
        unit_tokens = unit_tokens[1:]
    if len(unit_tokens) == 1 and unit_tokens[0] in _DIRECTIONAL_TOKENS:
        return normalized_street, None
    unit = " ".join(unit_tokens).strip() or None
    return base, unit


def _normalize_unit(value) -> str | None:
    """A HomeHarvest Address.unit ("Unit 439", "#12", "Lot 90") in the same form
    _split_unit gives a typed address's unit ("439", "12", "90"): normalized,
    leading unit keyword dropped. None when blank."""
    words = _normalize_street(value or "").split()
    if words and words[0] in _UNIT_KEYWORDS:
        words = words[1:]
    return " ".join(words) or None


def _normalize_street_suffix(street: str) -> str:
    """
    Abbreviate the street-TYPE word (Street->St, Avenue->Ave, ...) — only the
    last alphabetic token, never a word earlier in the name. Several map keys
    ("Cove", "Valley", "Vista", "Ranch", "Village", ...) are common street-NAME
    words too (e.g. "Desert Cove Ave", "Star Valley St"); abbreviating every
    match mangled those into "Desert Cv Ave", which no longer matched
    HomeHarvest's own (correctly spelled) result and broke the lookup.
    """
    words = street.split()
    for i in range(len(words) - 1, -1, -1):
        word = words[i]
        if word.isalpha():
            abbrev = _STREET_SUFFIX_MAP.get(word.lower())
            if abbrev:
                words[i] = abbrev
            break
    return " ".join(words)


def _query_parts(address: str) -> tuple[str, str]:
    """(query_address, normalized street) for a free-form "street, city, ST zip"
    address: the street part gets its type word abbreviated (see
    _normalize_street_suffix) before it's sent to Realtor.com's geocoder."""
    street_part, _, rest = address.strip().partition(",")
    normalized_street = _normalize_street_suffix(street_part)
    return normalized_street + ("," + rest if rest else ""), normalized_street


def subject_street_key(address: str) -> tuple[str, str | None]:
    """Normalized (base_street, unit) for an address's street part — the key
    used to recognise the subject among search results."""
    _query, street = _query_parts(address)
    return _split_unit(_normalize_street(street))


def match_property(address: str, candidates: list):
    """
    The candidate Property that IS `address`, tried by exact then fuzzy
    street match: (matched, fuzzy), or (None, False).

    Exact: same base street, and the same unit when the address names one —
    a sibling unit in the same building is never accepted. Fuzzy: one street
    starts with the other (dropped directional suffix, slight truncation),
    only when neither side names a unit or both name the same one.
    """
    target_base, target_unit = subject_street_key(address)
    exact_match = None
    fuzzy_match = None
    for prop in candidates:
        if not prop or not prop.address:
            continue
        result_street = _normalize_street(prop.address.street or "")
        if not result_street:
            continue
        result_unit = _normalize_unit(prop.address.unit)

        if result_street == target_base:
            if target_unit is None or result_unit == target_unit:
                exact_match = prop
                break
            # Same building, different (or unrecorded) unit — not safe to
            # treat as a match, keep looking rather than picking a sibling
            # unit's listing.
            continue

        if target_unit is not None and result_unit != target_unit:
            continue
        if result_street.startswith(target_base) or target_base.startswith(result_street):
            if fuzzy_match is None:
                fuzzy_match = prop

    matched = exact_match or fuzzy_match
    return matched, exact_match is None and fuzzy_match is not None


# Radius for the one fallback search when the direct lookup doesn't verify.
LOOKUP_FALLBACK_RADIUS_MILES = 0.05


def resolve_record(address: str) -> tuple[dict | None, dict | None]:
    """
    Find the Realtor.com record that IS `address`, whatever its status.

    1. Realtor.com's geocoder resolves the text to a property id; that home's
       record is fetched directly (works for sold, off-market, for-sale and
       pending homes alike) and must pass match_property — the geocoder
       happily returns a different street or unit for bad input.
    2. If that doesn't verify and the geocoder at least landed on an address,
       one radius search (all statuses) around that point, same matcher.

    Returns (listing, suggestion): listing is listing_from_property()'s dict
    or None; suggestion is the geocoder's result (or None), which callers can
    use as a building-level location when nothing verified.
    """
    if not address or not address.strip():
        return None, None
    query_address, _street = _query_parts(address)
    suggestion = client.suggest_location(query_address)
    if not suggestion or suggestion.get("area_type") != "address":
        return None, suggestion

    if suggestion.get("mpr_id"):
        home = client.fetch_home(suggestion["mpr_id"])
        matched, fuzzy = match_property(address, [home])
        if matched is not None:
            return listing_from_property(matched, fuzzy), suggestion

    centroid = suggestion.get("centroid") or {}
    if centroid.get("lat") is not None and centroid.get("lon") is not None:
        nearby = client.search_near(centroid["lat"], centroid["lon"], listing_type=None,
                                    radius_mi=LOOKUP_FALLBACK_RADIUS_MILES, limit=25)
        matched, fuzzy = match_property(address, nearby)
        if matched is not None:
            return listing_from_property(matched, fuzzy), suggestion

    log.info(f"No verified record for {address!r} (geocoder: {suggestion.get('text')!r})")
    return None, suggestion

def _compute_active_status(matched) -> tuple[bool, str]:
    """(active, mls_status) from a matched Property — used by listing_from_property."""
    # Use status (CONTINGENT, FOR_SALE, PENDING etc.) — more accurate than mls_status
    # which can say "Active" even when status is CONTINGENT
    raw_status = matched.status or matched.mls_status
    status_val = raw_status.value if hasattr(raw_status, "value") else str(raw_status or "UNKNOWN")

    status_upper = status_val.upper()
    mls_status_upper = (matched.mls_status or "").upper()

    # Fail if either field indicates the listing is no longer available
    status_inactive = status_upper in INACTIVE_STATUSES or mls_status_upper in INACTIVE_STATUSES
    status_active = status_upper in ACTIVE_STATUSES or mls_status_upper in ACTIVE_STATUSES
    return status_active and not status_inactive, status_val



# Statuses where the record's list price is a live asking price.
ON_MARKET_STATUSES = {"FOR_SALE", "ACTIVE", "PENDING", "CONTINGENT"}


def listing_from_property(matched, fuzzy: bool = False) -> dict:
    """
    A HomeHarvest Property as the listing dict the rest of the package
    expects: address, city, state, zip, sqft, beds, baths, lot_sqft,
    year_built, garage, stories, has_pool, has_basement, property_type,
    subdivision, special_listing_conditions, latitude, longitude — plus
    status (active, on_market, mls_status), listed_price (the record's last
    list price, which is stale for an off-market home; see on_market),
    last_sold_price/last_sold_date, description, property_url and
    address_fuzzy_match.
    """
    addr = matched.address
    desc = matched.description

    baths = None
    if desc and (desc.baths_full is not None or desc.baths_half is not None):
        baths = (desc.baths_full or 0) + (desc.baths_half or 0) * 0.5

    active, status_val = _compute_active_status(matched)
    has_pool, has_basement = extract_pool_basement(matched.tags)
    last_sold_price = (getattr(desc, "sold_price", None) if desc else None) or matched.last_sold_price
    last_sold_date = matched.last_sold_date

    return {
        "address":       addr.street if addr else None,
        "unit":          addr.unit if addr else None,
        "city":          addr.city if addr else None,
        "state":         addr.state if addr else None,
        "zip":           addr.zip if addr else None,
        "sqft":          desc.sqft if desc else None,
        "beds":          desc.beds if desc else None,
        "baths":         baths,
        "lot_sqft":      desc.lot_sqft if desc else None,
        "year_built":    desc.year_built if desc else None,
        "garage":        extract_garage(matched),
        "stories":       extract_stories(matched),
        "has_pool":      has_pool,
        "has_basement":  has_basement,
        "special_listing_conditions": extract_special_conditions(matched),
        "subdivision":   extract_subdivision(matched),
        # HomeHarvest's own vocabulary (e.g. "single_family") — comps.py's
        # _resolve_property_types() accepts this directly.
        "property_type": desc.type if desc else None,
        "listed_price":  matched.list_price,
        "last_sold_price": last_sold_price,
        "last_sold_date":  last_sold_date.date().isoformat() if hasattr(last_sold_date, "date") else None,
        "latitude":      matched.latitude,
        "longitude":     matched.longitude,
        "description":         getattr(desc, "text", None) if desc else None,
        "property_url":        str(matched.property_url) if matched.property_url else None,
        "mls_status":           status_val,
        "active":               active,
        "on_market":            status_val.upper() in ON_MARKET_STATUSES,
        "address_fuzzy_match":  fuzzy,
    }


def get_full_listing(address: str) -> dict | None:
    """The verified listing dict for `address` (see resolve_record), or None."""
    listing, _suggestion = resolve_record(address)
    return listing
