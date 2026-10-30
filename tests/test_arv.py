"""Tests for arv.py — ARV pool selection and aggregation on top of
scoring.score_all_comps_gated.
"""
import pytest

from arv_mcp.arv import (
    MARKET_BAND,
    MAX_DISTANCE_MI,
    MIN_SUBDIVISION_SALES,
    NEAREST_ANCHOR_N,
    RENOVATED_MIN_COMPS,
    CompVerdict,
    _comp_distance_mi,
    _subdivision_medians,
    _subject_anchor,
    _weighted_median,
    _weighted_percentile,
    classify_comps,
    comp_confidence_tier,
    estimate_arv_gated,
    renovated_range,
    select_eligible_comps,
)
from arv_mcp.scoring import score_all_comps_gated


def _listing(**kw):
    base = dict(
        sqft=2000, beds=3, baths=2, year_built=2015, lot_sqft=7000,
        property_type="Single Family (Residential)",
        latitude=33.5000, longitude=-112.0000,
    )
    base.update(kw)
    return base


def _comp(**kw):
    base = dict(
        address="test comp", sqft=2050, beds=3, baths=2, year_built=2016,
        lot_sqft=7200, price_sqft=250.0, days_ago=30, type="single_family",
        latitude=33.5006, longitude=-112.0000,  # ~0.04mi from the listing above
    )
    base.update(kw)
    return base


# ── pure helpers ──────────────────────────────────────────────────────────────

class TestCompDistanceMi:
    def test_missing_coords_is_none(self):
        assert _comp_distance_mi({"latitude": None, "longitude": None}, _comp()) is None
        assert _comp_distance_mi(_listing(), _comp(latitude=None)) is None

    def test_known_distance_is_positive(self):
        d = _comp_distance_mi(_listing(), _comp())
        assert 0 < d < 0.1


class TestWeightedMedian:
    def test_empty_or_zero_weight_is_none(self):
        assert _weighted_median([]) is None
        assert _weighted_median([(100.0, 0.0), (200.0, 0.0)]) is None

    def test_single_item(self):
        assert _weighted_median([(150.0, 10.0)]) == 150.0

    def test_heavier_weight_pulls_the_median(self):
        v = _weighted_median([(100.0, 1.0), (250.0, 90.0), (400.0, 1.0)])
        assert v == 250.0

    def test_equal_weights_is_plain_median_like(self):
        v = _weighted_median([(100.0, 1.0), (200.0, 1.0), (300.0, 1.0)])
        assert v == 200.0


class TestSubjectAnchor:
    def _scored(self, comps):
        return [(c, 0.0, [], True) for c in comps]

    def test_median_of_nearby_comps(self):
        comps = [_comp(price_sqft=p) for p in (240, 250, 260)]
        assert _subject_anchor(_listing(), self._scored(comps), {}) == 250

    def test_only_the_nearest_n_count(self):
        assert NEAREST_ANCHOR_N == 8
        near = [_comp(price_sqft=250, latitude=33.5000 + i * 0.0001) for i in range(NEAREST_ANCHOR_N)]
        far = [_comp(price_sqft=900, latitude=33.52 + i * 0.001) for i in range(10)]  # >1 mi out
        assert _subject_anchor(_listing(), self._scored(far + near), {}) == 250

    def test_ignores_comps_without_price_or_distance(self):
        comps = [_comp(price_sqft=300), _comp(price_sqft=310),
                 _comp(price_sqft=0), _comp(price_sqft=999, latitude=None)]
        assert _subject_anchor(_listing(), self._scored(comps), {}) == pytest.approx(305)

    def test_none_when_nothing_to_anchor_on(self):
        assert _subject_anchor(_listing(), self._scored([]), {}) is None
        assert _subject_anchor(_listing(latitude=None), self._scored([_comp()]), {}) is None

    def test_prefers_subjects_subdivision_median(self):
        comps = [_comp(price_sqft=p) for p in (240, 250, 260)]  # nearest-N would give 250
        listing = _listing(subdivision="Knoell Mesa Unit 1")
        assert _subject_anchor(listing, self._scored(comps), {"KNOELL MESA": 300.0}) == 300.0

    def test_falls_back_to_nearest_n_when_subdivision_thin_or_missing(self):
        comps = [_comp(price_sqft=p) for p in (240, 250, 260)]
        listing = _listing(subdivision="LONELY ACRES")
        assert _subject_anchor(listing, self._scored(comps), {"KNOELL MESA": 300.0}) == 250
        assert _subject_anchor(_listing(), self._scored(comps), {"KNOELL MESA": 300.0}) == 250


class TestSubdivisionMedians:
    def _scored(self, comps):
        return [(c, 0.0, [], True) for c in comps]

    def test_thin_subdivision_not_trusted(self):
        comps = [_comp(subdivision="SEVILLE", price_sqft=p) for p in (200, 210)]
        assert _subdivision_medians(self._scored(comps)) == {}

    def test_phases_pool_into_one_median(self):
        assert MIN_SUBDIVISION_SALES == 3
        comps = [_comp(subdivision=f"SEVILLE PARCEL {i}", price_sqft=p)
                 for i, p in enumerate((200, 210, 220))]
        assert _subdivision_medians(self._scored(comps)) == {"SEVILLE": 210}

    def test_ignores_comps_without_subdivision_or_price(self):
        comps = [_comp(subdivision="SEVILLE", price_sqft=p) for p in (200, 210, 220)]
        comps += [_comp(subdivision=None, price_sqft=999), _comp(subdivision="SEVILLE", price_sqft=0)]
        assert _subdivision_medians(self._scored(comps)) == {"SEVILLE": 210}


# ── integration: classify_comps / select_eligible_comps / estimate_arv_gated ──

def _rich_pool():
    """A subject whose nearby market runs ~$250/sqft, three nearby sales at
    ~$400/sqft (should fail the market band against the local anchor), a
    same-market comp that fails a physical gate, and a same-market comp
    that's too far to ever be eligible."""
    listing = _listing()
    same_market = [_comp(price_sqft=p) for p in (245, 250, 255)]          # eligible
    pricier_market = [_comp(price_sqft=p) for p in (395, 400, 405)]       # wrong value tier
    physical_fail = _comp(sqft=3800, price_sqft=252)                       # +90% sqft -> gated out
    too_far = _comp(price_sqft=248, latitude=33.5300)                      # ~2.1mi -> distance cap
    comps = same_market + pricier_market + [physical_fail, too_far]
    return listing, comps


class TestClassifyComps:
    def test_returns_a_verdict_per_scored_comp(self):
        listing, comps = _rich_pool()
        verdicts = classify_comps(listing, comps)
        assert len(verdicts) == len(comps)
        assert all(isinstance(v, CompVerdict) for v in verdicts)

    def test_eligible_verdicts_have_no_exclusion_reason(self):
        listing, comps = _rich_pool()
        for v in classify_comps(listing, comps):
            if v.eligible:
                assert v.exclusion_reason is None
            else:
                assert v.exclusion_reason  # non-empty string

    def test_physical_gate_failure_reason_names_the_factor(self):
        listing, comps = _rich_pool()
        verdicts = classify_comps(listing, comps)
        v = next(v for v in verdicts if v.comp["sqft"] == 3800)
        assert "Sqft" in v.exclusion_reason

    def test_wrong_market_tier_reason_cites_both_prices(self):
        listing, comps = _rich_pool()
        verdicts = classify_comps(listing, comps)
        v = next(v for v in verdicts if v.comp["price_sqft"] == 400)
        assert "price tier" in v.exclusion_reason
        assert "$400" in v.exclusion_reason or "400/sqft" in v.exclusion_reason

    def test_too_far_reason_cites_distance(self):
        listing, comps = _rich_pool()
        verdicts = classify_comps(listing, comps)
        v = next(v for v in verdicts if v.comp.get("latitude") == 33.5300)
        assert "mi away" in v.exclusion_reason

    def test_no_anchor_reason_applies_to_every_comp(self):
        # Every comp has unresolvable distance, so the nearest-N anchor has
        # nothing to work with -> anchor is None.
        listing = _listing()
        comps = [_comp(price_sqft=p, latitude=None, longitude=None) for p in (245, 250)]
        verdicts = classify_comps(listing, comps)
        assert verdicts  # comps were still scored
        assert all(not v.eligible and "micro-market" in v.exclusion_reason for v in verdicts)

    def test_empty_when_nothing_scored(self):
        assert classify_comps(_listing(), []) == []

    def test_comp_judged_by_its_subdivision_median_when_established(self):
        # The pricey comp's own $/sqft (330) is +32% off the ~250 anchor, but its
        # subdivision's median (260) is in band -> eligible. A cheap comp whose
        # subdivision runs pricey fails, and the reason names the subdivision.
        listing = _listing()
        local = [_comp(price_sqft=p) for p in (245, 250, 255)]
        mid = [_comp(subdivision="MID ACRES UNIT 1", price_sqft=p) for p in (240, 260, 330)]
        rich = [_comp(subdivision="RICH HILLS", price_sqft=p) for p in (255, 400, 410)]
        verdicts = {(v.comp.get("subdivision"), v.comp["price_sqft"]): v
                    for v in classify_comps(listing, local + mid + rich)}
        assert verdicts[("MID ACRES UNIT 1", 330)].eligible is True
        cheap_in_rich = verdicts[("RICH HILLS", 255)]
        assert cheap_in_rich.eligible is False
        assert "subdivision RICH HILLS" in cheap_in_rich.exclusion_reason


class TestSelectEligibleComps:
    def test_selects_only_same_market_close_matching_comps(self):
        listing, comps = _rich_pool()
        eligible = select_eligible_comps(listing, comps)
        assert len(eligible) == 3
        assert all(v.comp["price_sqft"] in (245, 250, 255) for v in eligible)
        assert all(v.eligible and v.exclusion_reason is None for v in eligible)

    def test_empty_when_nothing_to_anchor_on(self):
        assert select_eligible_comps(_listing(), []) == []

    def test_market_band_is_configurable_via_module_constant(self):
        assert MARKET_BAND == 0.20
        assert MAX_DISTANCE_MI == 1.5


class TestEstimateArvGated:
    def test_weighted_median_times_sqft(self):
        listing, comps = _rich_pool()
        result = estimate_arv_gated(listing, comps)
        assert result is not None
        assert result["price_sqft"] == pytest.approx(250, abs=5)
        assert result["arv"] == round(result["price_sqft"] * listing["sqft"])
        assert len(result["eligible"]) == 3

    def test_none_when_no_subject_sqft(self):
        listing, comps = _rich_pool()
        listing["sqft"] = 0
        assert estimate_arv_gated(listing, comps) is None

    def test_falls_back_to_area_anchor_when_nothing_eligible(self):
        listing = _listing()
        # Every comp fails on property type -> nothing eligible, but they're
        # still nearby and priced, so the local anchor exists.
        comps = [_comp(type="condos", price_sqft=p) for p in (245, 250, 255)]
        result = estimate_arv_gated(listing, comps)
        assert result is not None
        assert result["price_sqft"] == 250  # the local anchor, not an eligible-comp median
        assert result["arv"] == round(250 * listing["sqft"])
        assert result["eligible"] == []   # zero comps actually fed this number

    def test_none_when_nothing_eligible_and_no_anchor(self):
        # The comps' own coords are unresolvable -> the nearest-N anchor has
        # nothing to work with (same fixture as TestClassifyComps' no-anchor
        # case) -> genuinely nothing to fall back to.
        listing = _listing()
        comps = [_comp(price_sqft=p, latitude=None, longitude=None) for p in (245, 250)]
        assert estimate_arv_gated(listing, comps) is None

    def test_accepts_precomputed_classified_without_reclassifying(self):
        listing, comps = _rich_pool()
        classified = classify_comps(listing, comps)
        result = estimate_arv_gated(listing, comps, classified=classified)
        assert result is not None
        assert result["eligible"] == [v for v in classified if v.eligible]


class TestCompConfidenceTier:
    """comp_confidence_tier's nested radius/count rule, checked tightest-first:
    5@0.25mi -> tier 5, 5@0.5mi -> tier 4, 3@1.0mi -> tier 3, 1@1.5mi -> tier 2,
    nothing eligible -> tier 1."""

    def _verdict(self, dist):
        return CompVerdict(comp={}, eligible=True, gated_score=90.0, breakdown=[], distance_mi=dist,
                           exclusion_reason=None)

    def test_empty_pool_is_tier_1(self):
        assert comp_confidence_tier([]) == 1

    def test_five_within_quarter_mile_is_tier_5(self):
        pool = [self._verdict(0.1)] * 5
        assert comp_confidence_tier(pool) == 5

    def test_four_within_quarter_mile_falls_to_lower_ring(self):
        # Doesn't clear the 5@0.25mi bar, but does clear 5@0.5mi via the extra comp.
        pool = [self._verdict(0.1)] * 4 + [self._verdict(0.4)]
        assert comp_confidence_tier(pool) == 4

    def test_three_within_mile_only_is_tier_3(self):
        pool = [self._verdict(0.8)] * 3
        assert comp_confidence_tier(pool) == 3

    def test_single_comp_near_max_distance_is_tier_2(self):
        pool = [self._verdict(1.4)]
        assert comp_confidence_tier(pool) == 2

    def test_more_than_the_threshold_does_not_exceed_tier_5(self):
        pool = [self._verdict(0.05)] * 20
        assert comp_confidence_tier(pool) == 5

    def test_unknown_distance_only_cannot_be_placed_in_any_ring(self):
        pool = [self._verdict(None)] * 5
        assert comp_confidence_tier(pool) == 1

    def test_unknown_distance_comps_do_not_count_toward_known_rings(self):
        # 3 known comps at 0.1mi (not enough for tier 5 on their own) plus
        # unplaceable unknown-distance comps -> still capped by the known ones.
        pool = [self._verdict(0.1)] * 3 + [self._verdict(None)] * 10
        assert comp_confidence_tier(pool) == 3

    def test_mix_of_known_and_unknown_reaches_tier_2_floor(self):
        pool = [self._verdict(None)] * 5 + [self._verdict(1.3)]
        assert comp_confidence_tier(pool) == 2


# ── sanity: matches score_all_comps_gated's own eligibility flag ──────────────

def test_select_eligible_comps_agrees_with_score_all_comps_gated_gate():
    """Every comp select_eligible_comps returns must also be per-factor-gate
    eligible per score_all_comps_gated (the market-band check only narrows,
    never widens, what scoring already allowed through). Compared by
    price_sqft, which is unique across this fixture's comps."""
    listing, comps = _rich_pool()
    eligible_pps = {v.comp["price_sqft"] for v in select_eligible_comps(listing, comps)}
    scored = score_all_comps_gated(listing, comps)
    gate_eligible_pps = {c["price_sqft"] for c, _t, _b, ok in scored if ok}
    assert eligible_pps <= gate_eligible_pps


# ── renovated range / window parameters ──────────────────────────────────────

class TestRenovatedRange:
    def _v(self, pps, score=90.0):
        return CompVerdict(comp={"price_sqft": pps}, eligible=True, gated_score=score, breakdown=[],
                           distance_mi=0.1, exclusion_reason=None)

    def test_weighted_percentile(self):
        pairs = [(100.0, 1.0), (200.0, 1.0), (300.0, 1.0), (400.0, 1.0)]
        assert _weighted_percentile(pairs, 0.5) == 200.0
        assert _weighted_percentile(pairs, 0.75) == 300.0
        assert _weighted_percentile(pairs, 1.0) == 400.0
        assert _weighted_percentile([(1.0, 0.0)], 0.5) is None

    def test_upper_band_of_pool(self):
        pool = [self._v(p) for p in (200, 210, 220, 230, 240, 250, 260, 270, 280, 290)]
        rr = renovated_range(pool, 2000)
        assert (rr["low_price_sqft"], rr["high_price_sqft"]) == (270, 280)
        assert (rr["low"], rr["high"]) == (540000, 560000)

    def test_needs_enough_comps_and_sqft(self):
        assert renovated_range([self._v(250)] * (RENOVATED_MIN_COMPS - 1), 2000) is None
        assert renovated_range([self._v(250)] * RENOVATED_MIN_COMPS, 0) is None
        assert renovated_range([self._v(250, score=0.0)] * RENOVATED_MIN_COMPS, 2000) is None


class TestWindowParameters:
    def test_max_distance_follows_caller(self):
        listing = _listing()
        far = _comp(price_sqft=250, latitude=33.5300)  # ~2.1 mi
        comps = [_comp(price_sqft=p) for p in (245, 250, 255)] + [far]
        default = classify_comps(listing, comps)
        wide = classify_comps(listing, comps, max_distance_mi=3.0)
        assert next(v for v in default if v.comp is far).eligible is False
        assert "1.5 mi search radius" in next(v for v in default if v.comp is far).exclusion_reason
        assert next(v for v in wide if v.comp is far).eligible is True

    def test_recency_half_life_reaches_scoring(self):
        listing = _listing()
        old = _comp(price_sqft=250, days_ago=300)
        comps = [_comp(price_sqft=p) for p in (245, 250, 255)] + [old]
        assert next(v for v in classify_comps(listing, comps) if v.comp is old).eligible is False
        wide = classify_comps(listing, comps, recency_half_life_days=120)
        assert next(v for v in wide if v.comp is old).eligible is True
        est = estimate_arv_gated(listing, comps, max_distance_mi=1.5, recency_half_life_days=120)
        assert len(est["eligible"]) == 4
