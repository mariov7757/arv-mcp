"""Tests for scoring.py — the gated per-comp scorer: factor bands, the
per-factor floors that decide eligibility, and property-type normalization.
"""
import pytest

from arv_mcp.scoring import (
    ATTACHED_PROPERTY_TYPES,
    GATE_FLOOR_REF_DISTANCE_MI,
    GATE_FLOORS_FAR,
    GATE_FLOORS_FIXED,
    _gate_floor,
    _score_comp_gated,
    normalize_property_type,
    recency_half_life,
    score_all_comps_gated,
    subdivision_key,
)


class TestBreakdown:
    def test_factor_set(self):
        _total, breakdown, _eligible = _score_comp_gated({"sqft": 1500}, {"sqft": 1500}, 200.0)
        assert [r["factor"] for r in breakdown] == [
            "Distance", "Sqft", "Subdivision", "Condition", "Date sold", "Lot size", "Bed/Bath",
            "Year built", "Property type", "Garage", "Stories", "Pool", "Basement",
        ]

    def test_unknown_factors_excluded_from_total(self):
        """A comp identical on every known factor must not be docked for
        factors with no data on one side (known-weight rescale)."""
        listing = {"sqft": 1500, "beds": 3, "baths": 2}
        comp = {"sqft": 1500, "beds": 3, "baths": 2, "price_sqft": 200.0, "days_ago": 0}
        total, breakdown, _eligible = _score_comp_gated(listing, comp, median_price_sqft=200.0)
        distance = next(r for r in breakdown if r["factor"] == "Distance")
        assert distance["known"] is False
        assert total == 100.0  # unknowns excluded, not zeroed


class TestSubdivisionKey:
    @pytest.mark.parametrize("raw,expected", [
        ("KNOELL MESA UNIT 2", "KNOELL MESA"),
        ("Knoell Mesa Unit 1", "KNOELL MESA"),
        ("FULLER RANCH SUBDIVISION", "FULLER RANCH"),
        ("SEVILLE PARCEL 23", "SEVILLE"),
        ("POWER RANCH NEIGHBORHOOD 10 PARCEL B", "POWER RANCH"),
        ("Shiloh Ph 04 Sec 01", "SHILOH"),
        ("Cherry Creek 03", "CHERRY CREEK"),
        ("OLD WEST ESTATES NO. 3", "OLD WEST ESTATES"),
        ("CORDOVA AMD", "CORDOVA"),
        ("Sagewood (1-58)", "SAGEWOOD"),
        ("Settler's Meadow", "SETTLER S MEADOW"),
        ("SPECTRUM AT VAL VISTA PARCEL 1", "SPECTRUM AT VAL VISTA"),
        ("Star Valley Block 4", "STAR VALLEY"),
        ("", ""),
        (None, ""),
    ])
    def test_normalizes(self, raw, expected):
        assert subdivision_key(raw) == expected


class TestSubdivisionFactor:
    def _row(self, subject_sub, comp_sub):
        _total, breakdown, eligible = _score_comp_gated(
            {"sqft": 1500, "subdivision": subject_sub}, {"sqft": 1500, "subdivision": comp_sub}, 200.0)
        return next(r for r in breakdown if r["factor"] == "Subdivision"), eligible

    def test_same_subdivision_across_phases_is_full_credit(self):
        row, _ = self._row("KNOELL MESA UNIT 1", "Knoell Mesa Unit 2")
        assert (row["raw"], row["known"]) == (1.0, True)
        assert "same subdivision (KNOELL MESA)" in row["detail"]

    def test_different_subdivision_scores_zero_but_never_gates(self):
        row, eligible = self._row("SEVILLE PARCEL 23", "ASHLAND RANCH")
        assert (row["raw"], row["known"]) == (0.0, True)
        assert row["floor"] == 0.0
        assert eligible is True
        assert "different subdivision" in row["detail"]

    @pytest.mark.parametrize("subject_sub,comp_sub", [(None, "SEVILLE"), ("SEVILLE", None), (None, None), ("", "")])
    def test_unknown_when_either_side_missing(self, subject_sub, comp_sub):
        row, _ = self._row(subject_sub, comp_sub)
        assert row["known"] is False


class TestScoreAllCompsGated:
    def test_drops_comps_without_price_or_sqft_and_sorts_best_first(self):
        listing = {"sqft": 2000, "property_type": "single_family"}
        good = {"sqft": 2000, "price_sqft": 250.0, "days_ago": 10, "type": "single_family"}
        worse = {"sqft": 2500, "price_sqft": 250.0, "days_ago": 150, "type": "single_family"}
        unpriced = {"sqft": 2000, "price_sqft": 0}
        no_sqft = {"sqft": 0, "price_sqft": 250.0}
        scored = score_all_comps_gated(listing, [worse, unpriced, good, no_sqft])
        assert [c for c, *_ in scored] == [good, worse]

    def test_empty_when_nothing_valid(self):
        assert score_all_comps_gated({"sqft": 2000}, [{"sqft": 0, "price_sqft": 0}]) == []


class TestNormalizePropertyType:
    @pytest.mark.parametrize("raw,expected", [
        ("Single Family (Residential)", "single_family"),
        ("single_family", "single_family"),
        ("Condominium", "condos"),
        ("condo (residential)", "condos"),
        ("Townhouse", "townhomes"),
        ("Multi-Family (2-4 Units)", "multi_family"),
        ("Duplex", "multi_family"),
        ("Residential", "single_family"),   # generic catch-all
        ("", ""),
        (None, ""),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_property_type(raw) == expected


class TestScoreCompGated:
    """_score_comp_gated: non-compensatory floors on top of the tightened
    weights/bands — a comp failing any known factor's floor is ineligible
    regardless of how well it scores overall."""

    def _listing(self, **kw):
        base = dict(sqft=2000, beds=3, baths=2, year_built=2015, lot_sqft=7000,
                    property_type="Single Family (Residential)")
        base.update(kw)
        return base

    def _comp(self, **kw):
        base = dict(sqft=2050, beds=3, baths=2, year_built=2016, lot_sqft=7200,
                    price_sqft=250, days_ago=30, type="single_family")
        base.update(kw)
        return base

    def test_close_match_is_eligible(self):
        total, bd, eligible = _score_comp_gated(self._listing(), self._comp(), 250)
        assert eligible is True
        assert total > 70

    def test_large_year_gap_ineligible_despite_decent_total(self):
        total, bd, eligible = _score_comp_gated(self._listing(), self._comp(year_built=1950), 250)
        assert eligible is False
        assert total > 50  # would have looked "decent" under a plain threshold

    def test_large_sqft_gap_ineligible(self):
        total, bd, eligible = _score_comp_gated(self._listing(), self._comp(sqft=3200, beds=4, baths=3), 250)
        assert eligible is False

    def test_property_type_mismatch_ineligible(self):
        total, bd, eligible = _score_comp_gated(self._listing(), self._comp(type="condos"), 250)
        assert eligible is False

    def test_unknown_factor_never_gates(self):
        # No year_built on either side -> that factor is unknown, so it can't
        # fail the floor even though its "raw" default (0.5) is below GATE_FLOORS.
        total, bd, eligible = _score_comp_gated(
            self._listing(year_built=None), self._comp(year_built=None), 250)
        year_row = next(r for r in bd if r["factor"] == "Year built")
        assert year_row["known"] is False
        assert eligible is True

    def test_mild_mismatches_stay_eligible(self):
        # Within tolerance on every gated factor -> still eligible.
        total, bd, eligible = _score_comp_gated(
            self._listing(), self._comp(year_built=2022, sqft=2150), 250)
        assert eligible is True

    def test_floor_only_applies_to_configured_factors(self):
        # Condition/Bed-Bath/Garage/Stories/Pool/Basement have no floor (0.0
        # default) — confirm they're absent from both floor dicts (soft/
        # tiebreak only).
        for f in ("Condition", "Bed/Bath", "Garage", "Stories", "Pool", "Basement",
                  "Distance"):
            assert f not in GATE_FLOORS_FAR
            assert f not in GATE_FLOORS_FIXED

    def test_severe_bedbath_mismatch_does_not_gate(self):
        # HomeHarvest reports 0bd/0.0ba for a real chunk of comps rather than
        # leaving it blank -- a data gap, not a genuine studio -- so Bed/Bath
        # can't be allowed to exclude an otherwise-matching comp on its own.
        # Still scored (contributes to the total), just no floor to fail.
        total, bd, eligible = _score_comp_gated(
            self._listing(), self._comp(beds=0, baths=0), 250)
        bedbath_row = next(r for r in bd if r["factor"] == "Bed/Bath")
        assert bedbath_row["raw"] < 1.0   # real mismatch, reflected in the score
        assert bedbath_row["floor"] == 0.0
        assert eligible is True

    def test_unknown_distance_uses_conservative_far_floor(self):
        # No lat/lon on either side -> distance_mi is None -> _gate_floor must
        # fall back to the strict (far) floor, not silently loosen.
        total, bd, eligible = _score_comp_gated(self._listing(), self._comp(sqft=3050), 250)
        sqft_row = next(r for r in bd if r["factor"] == "Sqft")
        assert sqft_row["floor"] == pytest.approx(GATE_FLOORS_FAR["Sqft"])
        assert eligible is False  # 52% bigger fails even a loose floor, but confirms it's not 0

    def test_close_comp_gets_looser_floor_and_can_pass(self):
        # Same subject/comp as the size-mismatch case, but now ~0.05mi apart
        # (well inside GATE_FLOOR_REF_DISTANCE_MI) -> the sqft floor loosens
        # enough that a mismatch which fails at range now passes up close.
        listing = self._listing(latitude=33.5000, longitude=-112.0000)
        near_comp = self._comp(sqft=2450, latitude=33.5005, longitude=-112.0000)  # ~0.035mi
        far_comp  = self._comp(sqft=2450, latitude=33.5090, longitude=-112.0000)  # ~0.62mi
        _, near_bd, near_elig = _score_comp_gated(listing, near_comp, 250)
        _, far_bd, far_elig = _score_comp_gated(listing, far_comp, 250)
        near_floor = next(r for r in near_bd if r["factor"] == "Sqft")["floor"]
        far_floor = next(r for r in far_bd if r["factor"] == "Sqft")["floor"]
        assert near_floor < far_floor
        assert far_floor == pytest.approx(GATE_FLOORS_FAR["Sqft"])
        assert far_elig is False
        assert near_elig is True

    def test_property_type_floor_is_not_distance_scaled(self):
        # A condo right next door still isn't a comp for a SFR -- the fixed
        # floors ignore distance entirely.
        listing = self._listing(latitude=33.5000, longitude=-112.0000)
        near_condo = self._comp(type="condos", latitude=33.5001, longitude=-112.0000)  # ~0.007mi
        _, bd, eligible = _score_comp_gated(listing, near_condo, 250)
        type_row = next(r for r in bd if r["factor"] == "Property type")
        assert type_row["floor"] == GATE_FLOORS_FIXED["Property type"]
        assert eligible is False

    def test_attached_type_lot_mismatch_does_not_gate(self):
        # Real data: three identical same-street townhomes showed lot_sqft of
        # 365/395/437 against a subject's 4,493 -- measurement noise, not a
        # real difference. Lot size gets floor 0 for attached types so it
        # can't exclude an otherwise-matching comp.
        listing = self._listing(property_type="Townhouse (Residential)", lot_sqft=4493)
        comp = self._comp(type="townhomes", lot_sqft=400)
        total, bd, eligible = _score_comp_gated(listing, comp, 250)
        lot_row = next(r for r in bd if r["factor"] == "Lot size")
        assert lot_row["raw"] == 0.0   # still a huge % difference...
        assert lot_row["floor"] == 0.0   # ...but no floor to fail
        assert eligible is True

    def test_single_family_lot_mismatch_still_gates(self):
        # Same huge lot gap, but the subject is a plain SFR -- the exemption
        # doesn't apply, so the normal (here: unknown-distance -> far) floor
        # still excludes it.
        listing = self._listing(lot_sqft=4493)  # default property_type = SFR
        comp = self._comp(lot_sqft=400)
        total, bd, eligible = _score_comp_gated(listing, comp, 250)
        lot_row = next(r for r in bd if r["factor"] == "Lot size")
        assert lot_row["floor"] == pytest.approx(GATE_FLOORS_FAR["Lot size"])
        assert eligible is False

    def test_attached_property_types_set(self):
        assert ATTACHED_PROPERTY_TYPES == {"townhomes", "condos", "multi_family", "mobile"}


class TestGateFloor:
    def test_fixed_factors_ignore_distance(self):
        assert _gate_floor("Property type", 0.0) == GATE_FLOORS_FIXED["Property type"]
        assert _gate_floor("Property type", 5.0) == GATE_FLOORS_FIXED["Property type"]

    def test_scaled_factor_at_zero_distance_is_zero(self):
        assert _gate_floor("Sqft", 0.0) == 0.0

    def test_scaled_factor_at_or_past_reference_distance_is_full(self):
        assert _gate_floor("Sqft", GATE_FLOOR_REF_DISTANCE_MI) == pytest.approx(GATE_FLOORS_FAR["Sqft"])
        assert _gate_floor("Sqft", GATE_FLOOR_REF_DISTANCE_MI * 3) == pytest.approx(GATE_FLOORS_FAR["Sqft"])

    def test_scaled_factor_midway_is_half(self):
        half = GATE_FLOOR_REF_DISTANCE_MI / 2
        assert _gate_floor("Sqft", half) == pytest.approx(GATE_FLOORS_FAR["Sqft"] / 2)

    def test_unknown_distance_is_conservative(self):
        assert _gate_floor("Sqft", None) == GATE_FLOORS_FAR["Sqft"]

    def test_unlisted_factor_has_no_floor(self):
        assert _gate_floor("Condition", 0.0) == 0.0
        assert _gate_floor("Condition", None) == 0.0


class TestRecencyHalfLife:
    def test_default_window_keeps_default_half_life(self):
        assert recency_half_life(180) == 60

    def test_scales_with_window(self):
        assert recency_half_life(365) == pytest.approx(121.67, abs=0.01)
        assert recency_half_life(90) == 30

    def test_gate_cutoff_stays_proportional(self):
        # The Date sold floor is 0.15; it should bite at ~91% of any window.
        import math
        for days in (90, 180, 365, 730):
            cutoff = -recency_half_life(days) * math.log(GATE_FLOORS_FIXED["Date sold"]) / 0.6931
            assert cutoff / days == pytest.approx(0.912, abs=0.01)

    def test_half_life_reaches_the_date_factor(self):
        listing, comp = {"sqft": 2000}, {"sqft": 2000, "price_sqft": 200.0, "days_ago": 300}

        def row(hl):
            return next(r for r in _score_comp_gated(listing, comp, 200.0, hl)[1] if r["factor"] == "Date sold")

        assert row(60)["raw"] < 0.15 < row(120)["raw"]
