"""
arv.py — ARV pool selection + aggregation, layered on top of scoring.py's
per-comp gated scoring (score_all_comps_gated).

Where scoring.py answers "how well does this ONE comp match," this module
answers "which comps get to vote on ARV, how much does each vote count, and
— for every comp that doesn't qualify — why not":

  1. Fingerprint each subdivision's median $/sqft (scoring.subdivision_key,
     subdivisions with >= MIN_SUBDIVISION_SALES priced comps), and anchor the
     subject to its own subdivision's median — or, when it has none, to the
     median $/sqft of its NEAREST_ANCHOR_N closest comps. Never its listed
     price, which for a distressed listing tells you nothing about retail
     value.
  2. classify_comps() is the one pass every other function here builds on:
     every comp gets a CompVerdict — eligible or not, and when it isn't, a
     human-readable exclusion_reason — so a subject's rejected comps are
     never a silent gap. A comp is ELIGIBLE only if it (a) is
     within MAX_DISTANCE_MI, (b) passes every one of scoring.py's per-factor
     floors, AND (c) its subdivision's median $/sqft (or, without one, its
     own $/sqft) is within MARKET_BAND of the subject's anchor — the
     micro-market check. This last check is what actually stops
     a comp that's otherwise a great match but sits in a pricier or cheaper
     pocket from feeding the ARV; distance and physical similarity alone
     don't catch that.
  3. estimate_arv_gated() — ARV as a similarity-weighted MEDIAN of $/sqft
     across the eligible pool (weight = each comp's gated total score) —
     robust to the rare comp that still slips through, unlike a plain mean.
     Falls back to the subject's own micro-market anchor when nothing is
     eligible, rather than no ARV at all — see its docstring.
  4. comp_confidence_tier() — a 1-5 "Comp Confidence" signal built from the
     eligible pool's distances alone: how far out you'd have to search to
     find enough corroborating comps. Deliberately not a blend with comp
     quality or price agreement — those answer different questions (how well
     each comp matches, where the subject falls in the finish/condition
     range) that a single confidence number would only obscure.

Deliberately layered ON TOP of scoring.py rather than folded into it —
scoring.py only ever answers "how well does this one comp match," never
"which comps get to vote." The underlying comp dict and its per-factor
breakdown stay plain dicts (scoring.py/comps.py's established shape) — only
the classification wrapper this module introduces gets a real type.
"""
import statistics
from collections import defaultdict
from dataclasses import dataclass

from .scoring import RECENCY_DECAY_DAYS, _haversine_miles, score_all_comps_gated, subdivision_key

# +/- how far a comp's area $/sqft can be from the subject's anchor and still
# count as "the same micro-market."
MARKET_BAND = 0.20

# A subdivision needs at least this many priced sold comps before its median
# $/sqft is trusted as an area fingerprint; thinner ones fall through to the
# nearest-comp anchor (subject) or the comp's own $/sqft (comp).
MIN_SUBDIVISION_SALES = 3

# Default eligibility cap: comps.py's default fetch radius. Callers that
# widen or narrow the search pass max_distance_mi to match it.
MAX_DISTANCE_MI = 1.5

# Subject anchor fallback: median $/sqft of the N nearest comps by distance.
# Small enough to stay local, large enough that one odd sale can't set it.
NEAREST_ANCHOR_N = 8


@dataclass
class CompVerdict:
    """One comp's ARV-pool classification: scoring.py's gated score plus
    whether it actually qualifies for the ARV pool and, if not, why. The row
    shape classify_comps() returns and every other function in this module
    builds on."""
    comp: dict
    eligible: bool
    gated_score: float
    breakdown: list[dict]
    distance_mi: float | None
    exclusion_reason: str | None  # None iff eligible


def _comp_distance_mi(listing: dict, comp: dict) -> float | None:
    """Great-circle distance in miles, or None if either side is missing
    lat/lon. Recomputed independently of scoring._score_comp_gated's own
    Distance factor (which only exposes it as a formatted detail string) —
    haversine is cheap, so this small duplication is simpler than parsing."""
    lat, lon = listing.get("latitude"), listing.get("longitude")
    clat, clon = comp.get("latitude"), comp.get("longitude")
    if lat is None or lon is None or clat is None or clon is None:
        return None
    return _haversine_miles(lat, lon, clat, clon)


def _subdivision_medians(scored: list[tuple]) -> dict[str, float]:
    """Median $/sqft per normalized subdivision, for subdivisions with >=
    MIN_SUBDIVISION_SALES priced comps. `scored` is score_all_comps_gated's
    output."""
    pps_by_sub = defaultdict(list)
    for comp, _total, _breakdown, _eligible in scored:
        pps = comp.get("price_sqft")
        key = subdivision_key(comp.get("subdivision"))
        if pps and key:
            pps_by_sub[key].append(pps)
    return {k: statistics.median(v) for k, v in pps_by_sub.items() if len(v) >= MIN_SUBDIVISION_SALES}


def _subject_anchor(listing: dict, scored: list[tuple], sub_med: dict[str, float]) -> float | None:
    """Subject's micro-market anchor $/sqft: its own subdivision's median when
    that subdivision has enough sales, else the median $/sqft of the
    NEAREST_ANCHOR_N closest scored comps (by distance), regardless of
    whether they're individually eligible. None when neither is possible."""
    anchor = sub_med.get(subdivision_key(listing.get("subdivision")))
    if anchor is not None:
        return anchor

    dated = []
    for comp, _total, _breakdown, _eligible in scored:
        pps = comp.get("price_sqft")
        if not pps:
            continue
        d = _comp_distance_mi(listing, comp)
        if d is not None:
            dated.append((d, pps))
    if not dated:
        return None
    dated.sort(key=lambda t: t[0])
    nearest_pps = [pps for _, pps in dated[:NEAREST_ANCHOR_N]]
    return statistics.median(nearest_pps)


def _weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Similarity-weighted median: sort by value, walk cumulative weight,
    return the value where it first reaches half the total weight. None if
    every weight is zero/negative (nothing to weight by)."""
    items = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in items)
    if total <= 0:
        return None
    cum = 0.0
    for v, w in items:
        cum += w
        if cum >= total / 2:
            return v
    return items[-1][0]


def _gate_failure_reason(breakdown: list[dict]) -> str:
    """Human-readable reason a comp failed scoring.py's per-factor gates —
    reuses each failing factor's own `detail` string prefixed with the factor
    name, so the wording matches the comp's per-factor breakdown."""
    fails = [row for row in breakdown if row["known"] and row["raw"] < row["floor"]]
    if not fails:
        return "did not qualify"  # defensive: shouldn't happen when gate_ok is False
    return "; ".join(f"{row['factor']}: {row['detail']}" for row in fails)


def _market_tier_reason(area_price_sqft: float, anchor: float, subdivision: str | None) -> str:
    pct = area_price_sqft / anchor - 1
    source = f"subdivision {subdivision}" if subdivision else "comp"
    return (f"different price tier ({source} ${area_price_sqft:,.0f}/sqft vs. subject's local "
            f"${anchor:,.0f}/sqft, {pct:+.0%})")


def classify_comps(listing: dict, comps: list[dict], scored: list[tuple] | None = None, *,
                   max_distance_mi: float = MAX_DISTANCE_MI,
                   recency_half_life_days: float = RECENCY_DECAY_DAYS) -> list[CompVerdict]:
    """
    The one pass every other function in this module builds on: every comp
    that scoring.py could score, wrapped in a CompVerdict — eligible or not,
    and when it isn't, a human-readable reason why.

    Pass a pre-computed `scored` (scoring.score_all_comps_gated's output)
    to avoid re-scoring when the caller already has it.

    Returns a list of CompVerdicts, best-first (same order score_all_comps_gated
    returns) — [] when the subject has no sqft/no comps to score at all, see
    scoring.score_all_comps_gated.
    """
    if scored is None:
        scored = score_all_comps_gated(listing, comps, recency_half_life_days)
    if not scored:
        return []

    sub_med = _subdivision_medians(scored)
    anchor = _subject_anchor(listing, scored, sub_med)

    out = []
    for comp, total, breakdown, gate_ok in scored:
        dist = _comp_distance_mi(listing, comp)
        reason = None
        eligible = False

        if anchor is None:
            reason = "no micro-market data available for this subject's area"
        elif dist is not None and dist > max_distance_mi:
            reason = f"{dist:.2f} mi away — beyond the {max_distance_mi:g} mi search radius"
        elif not gate_ok:
            reason = _gate_failure_reason(breakdown)
        else:
            sub = subdivision_key(comp.get("subdivision"))
            area_pps = sub_med.get(sub)
            if area_pps is None:
                sub, area_pps = None, comp["price_sqft"]
            if abs(area_pps / anchor - 1) > MARKET_BAND:
                reason = _market_tier_reason(area_pps, anchor, sub)
            else:
                eligible = True

        out.append(CompVerdict(
            comp=comp, eligible=eligible, gated_score=total, breakdown=breakdown,
            distance_mi=dist, exclusion_reason=reason,
        ))
    return out


def select_eligible_comps(listing: dict, comps: list[dict], scored: list[tuple] | None = None,
                          **window) -> list[CompVerdict]:
    """The eligible subset of classify_comps() — every returned verdict has
    eligible=True and exclusion_reason=None."""
    return [v for v in classify_comps(listing, comps, scored=scored, **window) if v.eligible]


def estimate_arv_gated(listing: dict, comps: list[dict], classified: list[CompVerdict] | None = None, *,
                       max_distance_mi: float = MAX_DISTANCE_MI,
                       recency_half_life_days: float = RECENCY_DECAY_DAYS) -> dict | None:
    """
    ARV as a similarity-weighted median $/sqft across the eligible pool,
    applied to the subject's sqft.

    When nothing is eligible, falls back to the subject's own micro-market
    anchor (its subdivision's median, else the nearest-comp median — the same
    anchor every comp gets compared against, see _subject_anchor) rather than
    returning no ARV at all: "no comp is similar enough to trust" and "this
    area has no data at all" are different situations, and the area's own going rate is still
    a better number than nothing. Zero comps actually fed it though — the
    returned "eligible" list is empty — so callers MUST flag it as a
    fallback, never present it as comp-backed.

    None only when the subject has no sqft, or there's truly nothing to
    anchor on anywhere (no priced comp carries a resolvable distance) — see
    classify_comps's "no micro-market data"
    exclusion_reason for the same condition.

    Pass a pre-computed `classified` (classify_comps()'s output) to avoid
    re-classifying when the caller already needs it for other purposes (e.g.
    reporting every comp's exclusion_reason). Falling back still costs a
    second classify_comps() call in that case (to recover the anchor, which
    classify_comps doesn't expose) — only paid on the empty-eligible path,
    not the common case.

    Returns {"arv": int, "price_sqft": float, "eligible": [CompVerdict, ...]}
    — "eligible" is classify_comps()'s eligible subset, so
    comp_confidence_tier can consume it directly (empty list on the fallback
    path, by construction).
    """
    sqft = listing.get("sqft") or 0
    if not sqft:
        return None
    if classified is None:
        classified = classify_comps(listing, comps, max_distance_mi=max_distance_mi,
                                    recency_half_life_days=recency_half_life_days)
    eligible = [v for v in classified if v.eligible]
    if eligible:
        price_sqft = _weighted_median([(v.comp["price_sqft"], v.gated_score) for v in eligible])
        if price_sqft is not None:
            return {"arv": round(price_sqft * sqft), "price_sqft": price_sqft, "eligible": eligible}

    scored = score_all_comps_gated(listing, comps, recency_half_life_days)
    if not scored:
        return None
    anchor = _subject_anchor(listing, scored, _subdivision_medians(scored))
    if anchor is None:
        return None
    return {"arv": round(anchor * sqft), "price_sqft": anchor, "eligible": []}


# Nested-radius rule for comp_confidence_tier(), checked tightest-radius-first
# as (min_count, radius_mi, tier) — the first ring an eligible pool clears
# enough comps within sets the tier. Calibrated against the real distribution
# of eligible-comp counts and distances across ~8,000 recent Phoenix-metro
# subjects (median 12 eligible comps, median 3rd-nearest at 0.20mi — this market is
# dense enough that naive round numbers like "3 comps within a quarter mile"
# land near the MEDIAN outcome, not a strong one). These cutoffs split that
# real population into a roughly even 30/23/28/15/4.5% spread across tiers
# 5-1, rather than one tier absorbing most listings.
CONFIDENCE_TIERS = [
    (5, 0.25, 5),
    (5, 0.5,  4),
    (3, 1.0,  3),
    (1, 1.5,  2),
]


def comp_confidence_tier(eligible: list[CompVerdict]) -> int:
    """
    Comp Confidence (1-5): how far out you'd have to search to find enough
    corroborating comps, via CONFIDENCE_TIERS' nested radius/count rule —
    "5+ comps within a quarter mile" is a 5, "had to reach past a mile for
    just 1" is a 2, nothing eligible at all is a 1. Deliberately not a
    weighted blend of count/proximity/quality into one formula: count and
    distance were never two things to reconcile against each other, they're
    one question ("is this a dense, well-supported market nearby"), and a
    plain nested threshold answers it in a sentence per tier instead of a
    number on an arbitrary scale. Comp quality and price agreement are
    excluded on purpose — those describe how well each comp matches and
    where the subject sits in the finish/condition spread, not how much
    corroboration the ARV has.

    A comp with unknown distance can't be placed in any ring, so it never
    counts toward a radius threshold — same as before, it just doesn't move
    the needle rather than being dropped or assumed close.

    `eligible`: classify_comps()'s eligible subset (or select_eligible_comps()/
    estimate_arv_gated()'s "eligible" output).
    """
    known_dists = [v.distance_mi for v in eligible if v.distance_mi is not None]
    for min_count, radius, tier in CONFIDENCE_TIERS:
        if sum(1 for d in known_dists if d <= radius) >= min_count:
            return tier
    return 2 if known_dists else 1


def _weighted_percentile(pairs: list[tuple[float, float]], q: float) -> float | None:
    """Weighted q-quantile (0 < q <= 1): sort by value, walk cumulative
    weight, return the first value whose cumulative weight reaches q of the
    total. None if every weight is zero/negative."""
    items = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in items)
    if total <= 0:
        return None
    cum = 0.0
    for v, w in items:
        cum += w
        if cum >= q * total:
            return v
    return items[-1][0]


# Renovated range: the upper part of the eligible pool, where finished-out
# sales sit. Needs enough comps for the top quartile to mean something.
RENOVATED_LOW_Q, RENOVATED_HIGH_Q = 0.75, 0.90
RENOVATED_MIN_COMPS = 5


def renovated_range(eligible: list[CompVerdict], sqft: int | None) -> dict | None:
    """
    {"low", "high", "low_price_sqft", "high_price_sqft"}: the similarity-
    weighted 75th-90th percentile $/sqft of the eligible pool x subject sqft.
    The weighted median (estimate_arv_gated) is typical-condition value; a
    flip finished to the top of the neighbourhood targets this range instead.
    None with fewer than RENOVATED_MIN_COMPS eligible comps or no sqft.
    """
    if not sqft or len(eligible) < RENOVATED_MIN_COMPS:
        return None
    pairs = [(v.comp["price_sqft"], v.gated_score) for v in eligible]
    low = _weighted_percentile(pairs, RENOVATED_LOW_Q)
    high = _weighted_percentile(pairs, RENOVATED_HIGH_Q)
    if low is None or high is None:
        return None
    return {"low": round(low * sqft), "high": round(high * sqft),
            "low_price_sqft": low, "high_price_sqft": high}
