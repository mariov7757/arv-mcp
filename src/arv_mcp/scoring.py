"""
scoring.py — Scores sold comps (from comps.py) against a subject listing.

Every candidate comp is scored 0-100 across a weighted set of comparability
factors, and each KNOWN factor must also clear a minimum raw match (its
"floor") for the comp to stay eligible for the ARV pool at all — a comp that
fails any one floor is excluded outright, no matter how strong it is
elsewhere. score_all_comps_gated() returns every comp best-first with its
per-factor breakdown and eligibility; arv.py builds the ARV pool from it.

Comps arrive with lat/lon (and city/state, from comps.py) from HomeHarvest;
the subject listing carries lat/lon from homeharvest.get_full_listing().
Either side missing lat/lon leaves Distance unknown, which drops it out of
the score rather than counting against the comp.

Subdivision comes from the MLS "Subdivision" line
(homeharvest.extract_subdivision), normalized by subdivision_key() so the
phases of one development ("KNOELL MESA UNIT 1" / "UNIT 2", "CHERRY CREEK PH
07 SEC 03") count as the same subdivision. It's a small tiebreak factor here
(same subdivision = full credit, different = none), known only when both
sides have one; arv.py also uses it to fingerprint each subdivision's median
$/sqft.

Pool/basement come from HomeHarvest's `tags` field (comps.py fetches
pydantic Property objects, not a DataFrame, specifically to expose this) via
homeharvest.extract_pool_basement(). Garage/stories come from
homeharvest.extract_garage/extract_stories — stories is the unit's MLS
"Levels", not Realtor.com's building-total stories.

Known gaps:
  - Arms-length filtering: each comp carries its MLS
    `special_listing_conditions` (e.g. "Lender Owned/REO", "Short Sale"),
    but REO/short-sale comps aren't excluded or down-weighted yet.
"""

import math
import re
import statistics

DISTANCE_DECAY_MILES = 0.25    # distance score halves roughly every this many miles
RECENCY_DECAY_DAYS = 60       # date-sold score halves roughly every this many days
DEFAULT_WINDOW_DAYS = 180     # the comp search window RECENCY_DECAY_DAYS is tuned for

# Garage tolerance band: full credit within the first number, zero credit by the second
GARAGE_FULL_DELTA, GARAGE_ZERO_DELTA = 0, 2

CONDITION_PENALTY_SCORE      = 0.3   # applied when a distress signal fires
CONDITION_PRICE_OUTLIER_PCT  = 0.15  # 15% below the pool's median $/sqft counts as a signal

# Reliable negative-condition signals only, matched against a comp's own
# listing description.
DISTRESS_KEYWORDS = {
    "fixer", "fixer-upper", "as-is", "as is", "tlc", "needs work",
    "needs updating", "needs renovation", "needs repair", "gut",
    "gut renovation", "foundation issue", "foundation problem",
    "fire damage", "flood damage", "water damage", "mold", "roof damage",
    "major repair", "investor special", "handyman special", "tear down",
    "teardown", "uninhabitable", "condemned", "cash only", "lot value",
    "sold for lot", "structure only", "land value",
}

# Plat/phase qualifiers MLSs append to a development's name. Everything from
# the first one onward is dropped, so "SEVILLE PARCEL 23", "POWER RANCH
# NEIGHBORHOOD 10 PARCEL B" and "SHILOH PH 04 SEC 01" reduce to SEVILLE, POWER
# RANCH and SHILOH. Live probes: grouping comps this way raised the share in
# subdivisions with >= 3 sales from 8/169 to 50/168 (Gilbert) and 41/173 to
# 126/173 (Austin).
_SUBDIVISION_QUALIFIER = re.compile(
    r"\b(UNIT|UNITS|PH|PHASE|SEC|SECTION|NO|NUMBER|PARCEL|PCL|NEIGHBORHOOD|LOTS?|BLOCK|BLK|"
    r"TRACT|PLAT|REPLAT|AMD|AMENDED|AMENDMENT|FILING|ADDITION|ADD|REV|REVISED)\b.*$")
_SUBDIVISION_WORD = re.compile(r"\bSUBDIVISION\b|\bSUBD?\b")
# Trailing phase markers with no keyword: "CHERRY CREEK 03", "SOMETHING II", "VISTA B".
_TRAILING_MARKERS = re.compile(r"(\s+([0-9]+[A-Z]?|[IVX]+|[A-Z]))+$")


def subdivision_key(raw) -> str:
    """Normalized subdivision for matching, or "" if blank. Uppercases, drops
    parenthesized lot ranges ("Sagewood (1-58)"), punctuation, the word
    SUBDIVISION, plat/phase qualifiers and trailing phase markers."""
    if not raw:
        return ""
    s = re.sub(r"\([^)]*\)", " ", str(raw).upper())
    s = re.sub(r"[^\w\s]", " ", s)
    s = _SUBDIVISION_WORD.sub(" ", s)
    s = _SUBDIVISION_QUALIFIER.sub("", s)
    s = _TRAILING_MARKERS.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def recency_half_life(days_back: int) -> float:
    """Date-sold half-life for a `days_back` comp window. The Date sold gate
    floor (0.15) on the default 60-day half-life rejects anything older than
    ~164 days, i.e. ~91% of the default 180-day window; scaling the
    half-life with the window keeps that cutoff proportional, so a wider
    window actually admits older sales instead of fetching and gating them."""
    return RECENCY_DECAY_DAYS * days_back / DEFAULT_WINDOW_DAYS


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _decay_score(value: float, half_life: float) -> float:
    """Exponential decay: 1.0 at value=0, 0.5 at value=half_life, tapering after."""
    if value <= 0:
        return 1.0
    return math.exp(-0.6931 * value / half_life)


def _tolerance_score(diff: float, full_credit: float, zero_credit: float) -> float:
    """1.0 within full_credit, linearly tapering to 0.0 at zero_credit."""
    diff = abs(diff)
    if diff <= full_credit:
        return 1.0
    if diff >= zero_credit:
        return 0.0
    return 1.0 - (diff - full_credit) / (zero_credit - full_credit)


# ── Gated scoring ─────────────────────────────────────────────────────────────
#
# Pool = every comp that passes every factor's FLOOR (the minimum raw match
# required to stay eligible at all), ARV = a similarity-weighted MEDIAN of
# $/sqft over that pool (see arv.py). A comp failing a floor is excluded
# outright — no amount of strength elsewhere buys it back — rather than a
# single weighted-average total, which would let a comp be very dissimilar on
# one axis (wrong size, wrong era) and still score well by being fine on
# everything else.
#
# Distance and subdivision carry only a small tiebreak weight here and no
# floor of their own: location and value tier are hard-gated separately (the
# search radius, and arv.py's micro-market check comparing each comp's
# subdivision or own $/sqft to the subject's local anchor) — this scorer's job is narrower: "is it the same *kind* of
# house," not "is it close" or "is it the same price tier." Year built is a real, tightly-banded
# factor, so a 1920s home and a 2020s home can't qualify as comps for each
# other.

GATE_W_SQFT          = 17
GATE_W_YEAR_BUILT    = 14
GATE_W_PROPERTY_TYPE = 8
GATE_W_BED_BATH      = 8
GATE_W_LOT_SIZE      = 8
GATE_W_CONDITION     = 8
GATE_W_DATE_SOLD      = 8
GATE_W_GARAGE        = 3
GATE_W_STORIES       = 2
GATE_W_POOL          = 2
GATE_W_BASEMENT      = 2
GATE_W_DISTANCE      = 5   # tiebreak only — distance is already hard-capped by the search radius
GATE_W_SUBDIVISION   = 5   # tiebreak only — value tier is already hard-gated by the micro-market check

# Tolerance bands: full credit within the first number, zero credit by the
# second. Tight enough that a floor actually has teeth.
GATE_SQFT_FULL_PCT,     GATE_SQFT_ZERO_PCT     = 0.10, 0.30
GATE_LOT_FULL_PCT,      GATE_LOT_ZERO_PCT      = 0.20, 0.50
GATE_YEAR_FULL_YEARS,   GATE_YEAR_ZERO_YEARS   = 6, 15
GATE_BEDBATH_FULL_DELTA, GATE_BEDBATH_ZERO_DELTA = 1, 3

# Minimum raw match (0-1) a KNOWN factor must clear to stay eligible; a comp
# failing any one of these is excluded from the ARV pool outright. Factors
# absent from both dicts below float at 0.0 (no floor — soft/tiebreak only):
# Condition (too unreliable a signal to gate on — marketing text/price-outlier
# heuristic), Bed/Bath (HomeHarvest reports 0bd/0.0ba for a real chunk of
# comps rather than leaving it blank — a data gap, not a genuine studio —
# so treating it as known-and-failing was gating out real comps on bad
# data; still scored, just can't exclude on its own), Garage/Stories/Pool/
# Basement (minor value drivers), Distance/Subdivision (already hard-gated
# elsewhere, see the Gated scoring note above).
#
# Sqft/Year built/Lot size are DISTANCE-SCALED rather than flat: proximity is
# itself evidence of comparability (phased subdivisions, additions, remodels,
# infill all cluster close together), so a comp right next door earns real
# slack on physical similarity, while a comp reaching toward MAX_DISTANCE has
# to actually prove it's a close match. The floor ramps linearly from 0 at
# the subject's doorstep up to its GATE_FLOORS_FAR value by
# GATE_FLOOR_REF_DISTANCE_MI, and stays there beyond — see _gate_floor().
# Property type and recency are NOT distance-scaled: being close doesn't make
# a condo comparable to a SFR, or a stale sale fresh.
GATE_FLOOR_REF_DISTANCE_MI = 0.5

# Far-floor values, expressed here as what they mean in GATE_SQFT_*/
# GATE_YEAR_*/GATE_LOT_* terms once a comp reaches GATE_FLOOR_REF_DISTANCE_MI:
# Sqft 1.00 -> must be within GATE_SQFT_FULL_PCT (10%) — the full-credit band
# itself, no partial credit tolerated at range. Year built 0.40 -> up to ~11.4
# years apart (starts at GATE_YEAR_ZERO_YEARS' 15 years right at the subject's
# doorstep, same 0.40 far-floor proportion as before tightens it from there).
# Lot size 0.25 -> up to 42.5% apart.
GATE_FLOORS_FAR = {
    "Sqft":       1.00,
    "Year built": 0.40,
    "Lot size":   0.25,
}
GATE_FLOORS_FIXED = {
    "Property type": 1.0,   # binary — must match, or fail entirely
    "Date sold":     0.15,
}

# Lot size is unreliable for attached-product types: a townhome/condo/mobile's
# platted "lot" is often just the building footprint or a slice of shared HOA
# land, and MLS records it inconsistently — confirmed directly on real data:
# three literally-identical townhomes on the same street, same sqft/beds/year,
# showed lot_sqft of 365/395/437 against a same-street subject's 4,493. That's
# not a physical difference, it's measurement noise, so Lot size gets no floor
# at all (soft/tiebreak only, same treatment as Condition) whenever the
# SUBJECT is one of these types — by the time lot size is checked, Property
# Type has already forced the comp to match, so this covers both sides.
ATTACHED_PROPERTY_TYPES = {"townhomes", "condos", "multi_family", "mobile"}


def _gate_floor(factor: str, distance_mi: float | None) -> float:
    """Minimum raw match `factor` must clear at `distance_mi` from the subject
    to stay eligible. Unknown distance is treated conservatively — the full
    (far) floor applies, since closeness can't be verified."""
    if factor in GATE_FLOORS_FIXED:
        return GATE_FLOORS_FIXED[factor]
    far = GATE_FLOORS_FAR.get(factor)
    if far is None:
        return 0.0
    if distance_mi is None:
        return far
    frac = min(1.0, max(0.0, distance_mi) / GATE_FLOOR_REF_DISTANCE_MI)
    return far * frac

# Canonicalizes human-readable ("Single Family (Residential)") and HomeHarvest
# ("single_family") property-type vocabularies onto the same set, so a subject
# described either way can be compared directly against HomeHarvest comps.
# Order matters: checked top-to-bottom, first match wins.
_PROPERTY_TYPE_KEYWORDS = [
    ("single_family", ("single family", "single_family")),
    ("condos",        ("condo",)),
    ("townhomes",     ("townhome", "townhouse")),
    ("multi_family",  ("multi-family", "multi family", "multi_family", "duplex", "triplex")),
    ("mobile",        ("mobile", "manufactured")),
    ("farm",          ("farm",)),
    ("land",          ("land", "vacant land")),
    # Generic catch-all category ("Residential" with no more specific type
    # attached) — checked last so it never shadows a more specific match
    # above (e.g. "Single Family (Residential)" already hits "single family"
    # first). Almost always means SFR, and without this a bare "Residential"
    # fails to match any comp's HomeHarvest type and gates out the entire pool.
    ("single_family", ("residential",)),
]


def normalize_property_type(raw) -> str:
    """Canonical property-type key for either vocabulary, or "" if blank/unrecognized."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    for canon, keys in _PROPERTY_TYPE_KEYWORDS:
        if any(k in s for k in keys):
            return canon
    return s  # already-canonical HomeHarvest value we didn't need to remap


def _score_comp_gated(listing: dict, comp: dict, median_price_sqft: float,
                      recency_half_life_days: float = RECENCY_DECAY_DAYS) -> tuple[float, list[dict], bool]:
    """
    Score one comp using the gated weights/bands/floors above. Returns
    (total, breakdown, eligible) — eligible is False if any KNOWN factor's raw fell below its
    (possibly distance-scaled — see _gate_floor) floor, meaning the comp
    should be excluded from the ARV pool regardless of `total` (still
    computed/returned for debugging/display; each row also carries the
    `floor` that was actually applied to it).
    """
    subject_sqft  = listing.get("sqft") or 0
    subject_lat   = listing.get("latitude")
    subject_lon   = listing.get("longitude")

    if (subject_lat is not None and subject_lon is not None
            and comp.get("latitude") is not None and comp.get("longitude") is not None):
        distance_mi = _haversine_miles(subject_lat, subject_lon, comp["latitude"], comp["longitude"])
        distance_score = _decay_score(distance_mi, DISTANCE_DECAY_MILES)
        distance_detail = f"{distance_mi:.2f} mi apart"
        distance_known = True
    else:
        distance_mi = None  # unknown -> _gate_floor treats this conservatively (full floor)
        distance_score = 0.5
        distance_detail = "lat/lon unavailable for subject or comp"
        distance_known = False

    subject_sub = subdivision_key(listing.get("subdivision"))
    comp_sub    = subdivision_key(comp.get("subdivision"))
    if subject_sub and comp_sub:
        subdivision_score = 1.0 if subject_sub == comp_sub else 0.0
        subdivision_detail = (f"same subdivision ({comp_sub})" if subject_sub == comp_sub
                              else f"different subdivision (subject {subject_sub} vs comp {comp_sub})")
        subdivision_known = True
    else:
        subdivision_score = 0.5
        subdivision_detail = "subdivision unavailable for subject or comp"
        subdivision_known = False

    comp_sqft = comp.get("sqft", 0)
    if subject_sqft:
        sqft_pct = (comp_sqft - subject_sqft) / subject_sqft
        sqft_score = _tolerance_score(sqft_pct, GATE_SQFT_FULL_PCT, GATE_SQFT_ZERO_PCT)
        sqft_detail = f"subject {subject_sqft} vs comp {comp_sqft} sqft ({sqft_pct:+.1%})"
        sqft_known = True
    else:
        sqft_score = 0.5
        sqft_detail = "subject sqft unavailable"
        sqft_known = False

    subject_lot = listing.get("lot_sqft") or 0
    comp_lot    = comp.get("lot_sqft") or 0
    if subject_lot and comp_lot:
        lot_pct = (comp_lot - subject_lot) / subject_lot
        lot_score = _tolerance_score(lot_pct, GATE_LOT_FULL_PCT, GATE_LOT_ZERO_PCT)
        lot_detail = f"subject {subject_lot:,} vs comp {comp_lot:,} sqft ({lot_pct:+.1%})"
        lot_known = True
    else:
        lot_score = 0.5
        lot_detail = "lot size unavailable for subject or comp"
        lot_known = False

    subject_beds  = listing.get("beds") or 0
    subject_baths = listing.get("baths") or 0
    comp_beds     = comp.get("beds", 0)
    comp_baths    = comp.get("baths", 0)
    bedbath_delta = abs(comp_beds - subject_beds) + abs(comp_baths - subject_baths)
    bedbath_score = _tolerance_score(bedbath_delta, GATE_BEDBATH_FULL_DELTA, GATE_BEDBATH_ZERO_DELTA)
    bedbath_detail = f"subject {subject_beds}bd/{subject_baths}ba vs comp {comp_beds}bd/{comp_baths}ba"

    subject_year = listing.get("year_built") or 0
    comp_year    = comp.get("year_built") or 0
    if subject_year and comp_year:
        year_score = _tolerance_score(comp_year - subject_year, GATE_YEAR_FULL_YEARS, GATE_YEAR_ZERO_YEARS)
        year_detail = f"subject {subject_year} vs comp {comp_year}"
        year_known = True
    else:
        year_score = 0.5
        year_detail = "year built unavailable for subject or comp"
        year_known = False

    subject_type = normalize_property_type(listing.get("property_type"))
    comp_type    = normalize_property_type(comp.get("type"))
    if subject_type and comp_type:
        type_score = 1.0 if subject_type == comp_type else 0.0
        type_detail = f"subject {subject_type} vs comp {comp_type}"
        type_known = True
    else:
        type_score = 0.5
        type_detail = "property type unavailable for subject or comp"
        type_known = False

    subject_garage = listing.get("garage")
    comp_garage    = comp.get("garage")
    if subject_garage is not None and comp_garage is not None:
        garage_score = _tolerance_score(comp_garage - subject_garage, GARAGE_FULL_DELTA, GARAGE_ZERO_DELTA)
        garage_detail = f"subject {subject_garage} vs comp {comp_garage} spaces"
        garage_known = True
    else:
        garage_score = 0.5
        garage_detail = "garage spaces unavailable for subject or comp"
        garage_known = False

    subject_stories = listing.get("stories")
    comp_stories    = comp.get("stories")
    if subject_stories and comp_stories:
        stories_score = 1.0 if subject_stories == comp_stories else 0.0
        stories_detail = f"subject {subject_stories} vs comp {comp_stories}"
        stories_known = True
    else:
        stories_score = 0.5
        stories_detail = "stories unavailable for subject or comp"
        stories_known = False

    days_ago = comp.get("days_ago", 0)
    date_score = _decay_score(days_ago, recency_half_life_days)
    date_detail = f"sold {days_ago} days ago"

    desc = (comp.get("description_text") or "").lower()
    keyword_hit = any(kw in desc for kw in DISTRESS_KEYWORDS)
    price_sqft = comp.get("price_sqft") or 0
    price_outlier = (
        median_price_sqft > 0 and price_sqft > 0
        and price_sqft < median_price_sqft * (1 - CONDITION_PRICE_OUTLIER_PCT)
    )
    condition_score = CONDITION_PENALTY_SCORE if (keyword_hit or price_outlier) else 1.0
    condition_detail = (
        "distress keyword in listing description" if keyword_hit
        else f"${price_sqft:.0f}/sqft is >{CONDITION_PRICE_OUTLIER_PCT:.0%} below pool median" if price_outlier
        else "no distress signal"
    )

    subject_pool = listing.get("has_pool")
    comp_pool    = comp.get("has_pool")
    if subject_pool is not None and comp_pool is not None:
        pool_score = 1.0 if subject_pool == comp_pool else 0.0
        pool_detail = f"subject {'has' if subject_pool else 'no'} pool vs comp {'has' if comp_pool else 'no'} pool"
        pool_known = True
    else:
        pool_score = 0.5
        pool_detail = "pool data unavailable for subject or comp"
        pool_known = False

    subject_basement = listing.get("has_basement")
    comp_basement    = comp.get("has_basement")
    if subject_basement is not None and comp_basement is not None:
        basement_score = 1.0 if subject_basement == comp_basement else 0.0
        basement_detail = f"subject {'has' if subject_basement else 'no'} basement vs comp {'has' if comp_basement else 'no'} basement"
        basement_known = True
    else:
        basement_score = 0.5
        basement_detail = "basement data unavailable for subject or comp"
        basement_known = False

    breakdown = [
        {"factor": "Distance",       "weight": GATE_W_DISTANCE,      "raw": distance_score,     "detail": distance_detail,     "known": distance_known},
        {"factor": "Sqft",           "weight": GATE_W_SQFT,          "raw": sqft_score,         "detail": sqft_detail,         "known": sqft_known},
        {"factor": "Subdivision",    "weight": GATE_W_SUBDIVISION,   "raw": subdivision_score,  "detail": subdivision_detail,  "known": subdivision_known},
        {"factor": "Condition",      "weight": GATE_W_CONDITION,     "raw": condition_score,    "detail": condition_detail,    "known": True},
        {"factor": "Date sold",      "weight": GATE_W_DATE_SOLD,     "raw": date_score,         "detail": date_detail,         "known": True},
        {"factor": "Lot size",       "weight": GATE_W_LOT_SIZE,      "raw": lot_score,          "detail": lot_detail,          "known": lot_known},
        {"factor": "Bed/Bath",       "weight": GATE_W_BED_BATH,      "raw": bedbath_score,      "detail": bedbath_detail,      "known": True},
        {"factor": "Year built",     "weight": GATE_W_YEAR_BUILT,    "raw": year_score,         "detail": year_detail,         "known": year_known},
        {"factor": "Property type",  "weight": GATE_W_PROPERTY_TYPE, "raw": type_score,         "detail": type_detail,         "known": type_known},
        {"factor": "Garage",         "weight": GATE_W_GARAGE,        "raw": garage_score,       "detail": garage_detail,       "known": garage_known},
        {"factor": "Stories",        "weight": GATE_W_STORIES,       "raw": stories_score,      "detail": stories_detail,      "known": stories_known},
        {"factor": "Pool",           "weight": GATE_W_POOL,          "raw": pool_score,         "detail": pool_detail,         "known": pool_known},
        {"factor": "Basement",       "weight": GATE_W_BASEMENT,      "raw": basement_score,     "detail": basement_detail,     "known": basement_known},
    ]
    for row in breakdown:
        row["points"] = row["raw"] * row["weight"]
        if row["factor"] == "Lot size" and subject_type in ATTACHED_PROPERTY_TYPES:
            row["floor"] = 0.0
        else:
            row["floor"] = _gate_floor(row["factor"], distance_mi)

    eligible = not any(row["known"] and row["raw"] < row["floor"] for row in breakdown)

    known_weight = sum(row["weight"] for row in breakdown if row["known"])
    known_points = sum(row["points"] for row in breakdown if row["known"])
    total = (known_points / known_weight * 100) if known_weight > 0 else known_points
    return total, breakdown, eligible


def score_all_comps_gated(listing: dict, comps: list[dict],
                          recency_half_life_days: float = RECENCY_DECAY_DAYS,
                          ) -> list[tuple[dict, float, list[dict], bool]]:
    """Every candidate comp, best-first, each paired with (total, breakdown,
    eligible). Callers filter on `eligible`
    for the ARV pool rather than a fixed score threshold."""
    valid = [c for c in comps if c.get("price_sqft") and c.get("sqft")]
    if not valid:
        return []
    median_price_sqft = statistics.median(c["price_sqft"] for c in valid)
    scored = [(c, *_score_comp_gated(listing, c, median_price_sqft, recency_half_life_days)) for c in valid]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored
