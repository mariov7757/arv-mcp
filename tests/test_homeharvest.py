"""Tests for data/homeharvest.py — the HomeHarvest data layer (data/client.py mocked)."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from homeharvest.core.scrapers.models import ListingType

from arv_mcp.data import homeharvest as hhc

# ── get_comps / get_active_pending (coordinate searches via client) ───────────

class _SearchSpy:
    def __init__(self, results=None, fail_on=None):
        self.calls = []
        self.results = results if results is not None else {}
        self.fail_on = fail_on

    def __call__(self, lat, lon, **kw):
        self.calls.append(dict(lat=lat, lon=lon, **kw))
        lt = kw.get("listing_type")
        if self.fail_on is not None and lt == self.fail_on:
            raise RuntimeError("blocked")
        return list(self.results.get(lt, []))


class TestGetComps:
    def test_sold_search_around_coordinates_with_window(self, monkeypatch):
        spy = _SearchSpy({ListingType.SOLD: ["a", "b"]})
        monkeypatch.setattr(hhc.client, "search_near", spy)
        assert hhc.get_comps(33.4, -111.8, ["single_family"], 2000, radius_mi=2.0, days_back=365) == ["a", "b"]
        (call,) = spy.calls
        assert call["lat"] == 33.4 and call["lon"] == -111.8
        assert call["listing_type"] == ListingType.SOLD
        assert call["radius_mi"] == 2.0
        assert call["past_days"] == 365
        assert call["sort_by"] == "sold_date"
        assert call["property_types"] == ["single_family"]
        assert (call["sqft_min"], call["sqft_max"]) == (1200, 2800)  # +/-40%
        assert call["limit"] == hhc.COMP_FETCH_LIMIT

    def test_defaults_and_no_sqft_band(self, monkeypatch):
        spy = _SearchSpy()
        monkeypatch.setattr(hhc.client, "search_near", spy)
        assert hhc.get_comps(33.4, -111.8, None, None) == []
        (call,) = spy.calls
        assert (call["radius_mi"], call["past_days"]) == (hhc.COMP_RADIUS_MILES, hhc.COMP_DAYS)
        assert (call["sqft_min"], call["sqft_max"]) == (None, None)

    def test_upstream_errors_propagate(self, monkeypatch):
        monkeypatch.setattr(hhc.client, "search_near", _SearchSpy(fail_on=ListingType.SOLD))
        with pytest.raises(RuntimeError):
            hhc.get_comps(33.4, -111.8, None, 1500)


class TestGetActivePending:
    def test_two_separate_status_searches(self, monkeypatch):
        spy = _SearchSpy({ListingType.FOR_SALE: ["active"], ListingType.PENDING: ["pending"]})
        monkeypatch.setattr(hhc.client, "search_near", spy)
        result = hhc.get_active_pending(33.4, -111.8, ["condos"], 1000, radius_mi=1.0)
        assert sorted(result) == ["active", "pending"]
        by_type = {c["listing_type"]: c for c in spy.calls}
        assert set(by_type) == {ListingType.FOR_SALE, ListingType.PENDING}
        assert by_type[ListingType.FOR_SALE]["exclude_pending"] is True
        assert "exclude_pending" not in by_type[ListingType.PENDING]
        for c in spy.calls:
            assert c["radius_mi"] == 1.0
            assert c["property_types"] == ["condos"]
            assert (c["sqft_min"], c["sqft_max"]) == (600, 1400)
            assert c["limit"] == hhc.ACTIVE_PENDING_LIMIT

    def test_upstream_errors_propagate(self, monkeypatch):
        monkeypatch.setattr(hhc.client, "search_near", _SearchSpy(fail_on=ListingType.PENDING))
        with pytest.raises(RuntimeError):
            hhc.get_active_pending(33.4, -111.8, None, None)


# ── _normalize_street ─────────────────────────────────────────────────────────

class TestNormalizeStreet:
    def test_lowercases_and_strips_punctuation(self):
        assert hhc._normalize_street("123 Main St.,") == "123 main st"

    def test_collapses_whitespace(self):
        assert hhc._normalize_street("123   Main   St") == "123 main st"


# ── _normalize_street_suffix ──────────────────────────────────────────────────

class TestNormalizeStreetSuffix:
    def test_abbreviates_trailing_suffix(self):
        assert hhc._normalize_street_suffix("123 Main Street") == "123 Main st"

    def test_leaves_already_abbreviated_suffix_unchanged(self):
        assert hhc._normalize_street_suffix("1317 E DESERT COVE AVE") == "1317 E DESERT COVE AVE"

    def test_does_not_mangle_suffix_word_used_mid_name(self):
        # "Cove" is a _STREET_SUFFIX_MAP key, but here it's part of the street
        # NAME ("Desert Cove"), not the trailing street TYPE — must stay put.
        assert hhc._normalize_street_suffix("Desert Cove Avenue") == "Desert Cove ave"

    def test_trailing_unit_number_does_not_block_suffix_abbreviation(self):
        assert hhc._normalize_street_suffix("10019 E Mountain View Road 1103") == "10019 E Mountain View rd 1103"


# ── _split_unit ────────────────────────────────────────────────────────────────

class TestSplitUnit:
    def test_bare_trailing_number_is_the_unit(self):
        assert hhc._split_unit("400 tower st 601") == ("400 tower st", "601")

    def test_no_trailing_token_means_no_unit(self):
        assert hhc._split_unit("400 tower st") == ("400 tower st", None)

    def test_unit_keyword_is_dropped(self):
        assert hhc._split_unit("400 tower st unit 601") == ("400 tower st", "601")

    def test_no_recognized_suffix_word_leaves_string_intact(self):
        assert hhc._split_unit("123 broadway") == ("123 broadway", None)

    def test_trailing_post_directional_is_not_a_unit(self):
        # "...Dr N" is a USPS post-directional street continuation, not a
        # condo/townhome unit -- must not be split off.
        assert hhc._split_unit("14817 w escondido dr n") == ("14817 w escondido dr n", None)

    def test_multi_token_unit_is_not_mistaken_for_directional(self):
        assert hhc._split_unit("6262 e brown rd 17 3") == ("6262 e brown rd", "17 3")


# ── address matching, record resolution, listing building ──────────────────

def _prop(street="123 Main St", unit=None, status="ACTIVE", mls_status="ACTIVE",
          desc_text="Nice house", desc_type="single_family",
          property_url="https://example.com/123", details=None,
          tags=None, latitude=32.2, longitude=-110.9, list_price=250000,
          days_on_mls=15, last_sold_price=None, last_sold_date=None):
    address = (SimpleNamespace(street=street, unit=unit, city="Tucson", state="AZ", zip="85701")
               if street is not None else None)
    description = SimpleNamespace(
        text=desc_text, type=desc_type, sqft=1500, beds=3, baths_full=2,
        baths_half=1, lot_sqft=6000, year_built=1985, stories=None, garage=None, sold_price=None,
    )
    return SimpleNamespace(
        address=address, status=status, mls_status=mls_status, description=description,
        property_url=property_url, details=details or [],
        # tags is where HomeHarvest's pool/basement flags live -- see
        # extract_pool_basement(), which listing_from_property always calls.
        tags=tags or [], latitude=latitude, longitude=longitude, list_price=list_price,
        days_on_mls=days_on_mls, last_sold_price=last_sold_price, last_sold_date=last_sold_date,
    )


class TestNormalizeUnit:
    @pytest.mark.parametrize("raw,expected", [
        ("Unit 439", "439"), ("#12", "12"), ("Apt 3B", "3b"), ("Lot 90", "90"), ("601", "601"),
        ("", None), (None, None),
    ])
    def test_normalize(self, raw, expected):
        assert hhc._normalize_unit(raw) == expected


class TestMatchProperty:
    def test_exact_match(self):
        prop = _prop(street="123 Main St")
        assert hhc.match_property("123 Main St, Tucson, AZ", [prop]) == (prop, False)

    def test_spelled_out_suffix_matches(self):
        prop = _prop(street="123 Main St")
        assert hhc.match_property("123 Main Street, Tucson, AZ", [prop]) == (prop, False)

    def test_fuzzy_match_dropped_directional_suffix(self):
        prop = _prop(street="123 Main St")
        assert hhc.match_property("123 Main, Tucson, AZ", [prop]) == (prop, True)

    def test_different_street_is_no_match(self):
        assert hhc.match_property("123 Main St, Tucson, AZ", [_prop(street="2000 W Elm St")]) == (None, False)

    def test_skips_none_and_streetless_candidates(self):
        good = _prop(street="Main St")
        assert hhc.match_property("Main St, Tucson, AZ", [None, _prop(street=""), _prop(street=None), good]) == (good, False)

    def test_unit_query_matches_only_same_unit(self):
        # MLS "Short Address" bakes the unit onto the street text; a query
        # like that must not resolve to a different unit in the same building.
        wrong, right = _prop(street="400 Tower St", unit="305"), _prop(street="400 Tower St", unit="601")
        assert hhc.match_property("400 Tower St 601, Phoenix, AZ 85004", [wrong, right]) == (right, False)

    def test_unit_keyword_on_both_sides_matches(self):
        # HomeHarvest's Address.unit is "Unit 601"; the typed address says "Unit 601".
        prop = _prop(street="400 Tower St", unit="Unit 601")
        assert hhc.match_property("400 Tower St Unit 601, Phoenix, AZ", [prop]) == (prop, False)

    def test_sibling_unit_is_rejected(self):
        sibling = _prop(street="500 Park Ln", unit="Unit 439")
        assert hhc.match_property("500 Park Ln Unit 1433, Mesa, AZ", [sibling]) == (None, False)


class TestResolveRecord:
    def _client(self, monkeypatch, suggestion=None, home=None, nearby=(), calls=None):
        calls = calls if calls is not None else []
        monkeypatch.setattr(hhc.client, "suggest_location", lambda q: calls.append(("suggest", q)) or suggestion)
        monkeypatch.setattr(hhc.client, "fetch_home", lambda pid: calls.append(("home", pid)) or home)
        monkeypatch.setattr(hhc.client, "search_near",
                            lambda lat, lon, **kw: calls.append(("near", lat, lon, kw)) or list(nearby))
        return calls

    _addr_suggestion = {"area_type": "address", "mpr_id": "M1", "text": "123 Main St, Tucson, AZ",
                        "centroid": {"lat": 32.2, "lon": -110.9}}

    def test_blank_address(self, monkeypatch):
        calls = self._client(monkeypatch)
        assert hhc.resolve_record("  ") == (None, None)
        assert calls == []

    def test_verified_direct_lookup(self, monkeypatch):
        calls = self._client(monkeypatch, self._addr_suggestion, home=_prop(street="123 Main St", status="SOLD"))
        listing, suggestion = hhc.resolve_record("123 Main Street, Tucson, AZ 85701")
        assert listing["address"] == "123 Main St"
        assert listing["mls_status"] == "SOLD"
        assert suggestion is self._addr_suggestion
        assert calls[0] == ("suggest", "123 Main st, Tucson, AZ 85701")  # suffix abbreviated for the geocoder
        assert [c[0] for c in calls] == ["suggest", "home"]  # no fallback search needed

    def test_non_address_geocode_is_unverified_without_more_requests(self, monkeypatch):
        # "99999 E Main St, Mesa" geocodes to the city -- never trust it.
        city = {"area_type": "city", "mpr_id": None, "text": "Mesa, AZ", "centroid": {"lat": 33.4, "lon": -111.7}}
        calls = self._client(monkeypatch, city)
        assert hhc.resolve_record("99999 E Main St, Mesa, AZ") == (None, city)
        assert [c[0] for c in calls] == ["suggest"]

    def test_no_geocode_result(self, monkeypatch):
        self._client(monkeypatch, None)
        assert hhc.resolve_record("nowhere") == (None, None)

    def test_wrong_property_falls_back_to_radius_search(self, monkeypatch):
        right = _prop(street="123 Main St", unit="Unit 2")
        calls = self._client(monkeypatch, self._addr_suggestion, home=_prop(street="123 Main St", unit="Unit 9"),
                             nearby=[_prop(street="125 Main St"), right])
        listing, _ = hhc.resolve_record("123 Main St Unit 2, Tucson, AZ")
        assert listing["unit"] == "Unit 2"
        near = [c for c in calls if c[0] == "near"][0]
        assert near[1:3] == (32.2, -110.9)
        assert near[3]["listing_type"] is None
        assert near[3]["radius_mi"] == hhc.LOOKUP_FALLBACK_RADIUS_MILES

    def test_nothing_verifies(self, monkeypatch):
        self._client(monkeypatch, self._addr_suggestion, home=_prop(street="999 Other Rd"),
                     nearby=[_prop(street="998 Other Rd")])
        assert hhc.resolve_record("123 Main St, Tucson, AZ") == (None, self._addr_suggestion)

    def test_missing_home_record(self, monkeypatch):
        self._client(monkeypatch, self._addr_suggestion, home=None, nearby=[])
        assert hhc.resolve_record("123 Main St, Tucson, AZ")[0] is None

    def test_get_full_listing_wraps_resolve_record(self, monkeypatch):
        self._client(monkeypatch, self._addr_suggestion, home=_prop(street="123 Main St"))
        assert hhc.get_full_listing("123 Main St, Tucson, AZ")["address"] == "123 Main St"

    def test_upstream_errors_propagate(self, monkeypatch):
        def boom(q):
            raise RuntimeError("blocked")
        monkeypatch.setattr(hhc.client, "suggest_location", boom)
        with pytest.raises(RuntimeError):
            hhc.resolve_record("123 Main St, Tucson, AZ")


class TestListingFromProperty:
    def test_subject_facts(self):
        result = hhc.listing_from_property(_prop(street="Main St"))
        assert result["address"] == "Main St"
        assert (result["city"], result["state"], result["zip"]) == ("Tucson", "AZ", "85701")
        assert (result["sqft"], result["beds"], result["baths"]) == (1500, 3, 2.5)
        assert (result["lot_sqft"], result["year_built"]) == (6000, 1985)
        assert result["property_type"] == "single_family"
        assert result["listed_price"] == 250000
        assert (result["latitude"], result["longitude"]) == (32.2, -110.9)
        assert result["active"] is True and result["on_market"] is True
        assert result["mls_status"] == "ACTIVE"
        assert result["description"] == "Nice house"
        assert result["property_url"] == "https://example.com/123"
        assert result["address_fuzzy_match"] is False

    def test_fuzzy_flag_passed_through(self):
        assert hhc.listing_from_property(_prop(), fuzzy=True)["address_fuzzy_match"] is True

    @pytest.mark.parametrize("status,active,on_market", [
        ("PENDING", False, True), ("CONTINGENT", False, True), ("SOLD", False, False),
        ("OFF_MARKET", False, False), ("SOMETHING_ELSE", False, False),
    ])
    def test_status_flags(self, status, active, on_market):
        result = hhc.listing_from_property(_prop(status=status, mls_status=status))
        assert (result["active"], result["on_market"], result["mls_status"]) == (active, on_market, status)

    def test_status_is_enum_like_with_value(self):
        prop = _prop()
        prop.status = SimpleNamespace(value="ACTIVE")
        assert hhc.listing_from_property(prop)["mls_status"] == "ACTIVE"

    def test_last_sale(self):
        sold = _prop(status="SOLD", last_sold_price=318000, last_sold_date=datetime(2021, 8, 11, 0, 0))
        result = hhc.listing_from_property(sold)
        assert (result["last_sold_price"], result["last_sold_date"]) == (318000, "2021-08-11")

    def test_no_description_object(self):
        prop = _prop()
        prop.description = None
        result = hhc.listing_from_property(prop)
        for key in ("description", "property_type", "sqft", "baths", "lot_sqft", "year_built", "stories"):
            assert result[key] is None

    def test_no_property_url(self):
        assert hhc.listing_from_property(_prop(property_url=None))["property_url"] is None

    def test_levels_garage_pool_conditions_and_subdivision(self):
        prop = _prop(tags=["swimming_pool"], details=[{"text": [
            "Levels: 1", "Building Total Stories: 4", "Garage Spaces: 2",
            "Special Listing Conditions: Lender Owned/REO", "Subdivision: SEVILLE PARCEL 23",
        ]}])
        result = hhc.listing_from_property(prop)
        assert result["stories"] == 1
        assert result["garage"] == 2.0
        assert result["has_pool"] is True
        assert result["has_basement"] is False
        assert result["special_listing_conditions"] == ["Lender Owned/REO"]
        assert result["subdivision"] == "SEVILLE PARCEL 23"


# ── stories / garage / special-conditions extraction ──────────────────────────

def _detail_prop(lines=None, stories=None, garage=None, tags=None, description=True):
    desc = SimpleNamespace(stories=stories, garage=garage) if description else None
    return SimpleNamespace(details=[{"text": lines}] if lines else [], description=desc, tags=tags)


class TestExtractStories:
    def test_unit_levels_beat_building_total_stories(self):
        # Condo: 1-level unit in a 4-story building -- description.stories is
        # the building's height, the Levels line is the unit's.
        assert hhc.extract_stories(_detail_prop(["Levels: 1"], stories=4)) == 1

    def test_level_word_parsed(self):
        assert hhc.extract_stories(_detail_prop(["Levels: Two"], stories=3)) == 2

    def test_levels_capped_at_building_total(self):
        # Live typo: "Levels: 14" on a 1-story single-family home.
        assert hhc.extract_stories(_detail_prop(["Levels: 14"], stories=1)) == 1

    def test_levels_used_as_is_without_building_total(self):
        assert hhc.extract_stories(_detail_prop(["Levels: 2"])) == 2

    def test_falls_back_to_description_without_levels_line(self):
        assert hhc.extract_stories(_detail_prop(["Other: x"], stories=2)) == 2

    def test_zero_or_unparseable_levels_fall_back(self):
        assert hhc.extract_stories(_detail_prop(["Levels: 0"], stories=1)) == 1
        assert hhc.extract_stories(_detail_prop(["Levels: Split Level"], stories=2)) == 2

    def test_none_when_nothing_usable(self):
        assert hhc.extract_stories(_detail_prop(description=False)) is None
        assert hhc.extract_stories(_detail_prop(stories=0)) is None


class TestExtractGarage:
    def test_garage_spaces_line_beats_description(self):
        assert hhc.extract_garage(_detail_prop(["Garage Spaces: 6"], garage=4.0)) == 6.0

    def test_unparseable_line_falls_back_to_description(self):
        assert hhc.extract_garage(_detail_prop(["Garage Spaces: lots"], garage=2.0)) == 2.0

    def test_description_used_without_line(self):
        assert hhc.extract_garage(_detail_prop(garage=3)) == 3.0

    def test_garage_tag_is_lower_bound(self):
        tags = ["garage_1_or_more", "garage_2_or_more"]
        assert hhc.extract_garage(_detail_prop(tags=tags)) == 2.0

    def test_carport_only_is_zero(self):
        assert hhc.extract_garage(_detail_prop(tags=["carport", "central_air"])) == 0.0

    def test_no_count_and_no_tags_is_unknown(self):
        assert hhc.extract_garage(_detail_prop(tags=[])) is None
        assert hhc.extract_garage(_detail_prop(description=False)) is None


class TestExtractSubdivision:
    def test_reads_line_as_written(self):
        assert hhc.extract_subdivision(_detail_prop(["Subdivision: KNOELL MESA UNIT 2"])) == "KNOELL MESA UNIT 2"

    @pytest.mark.parametrize("value", ["N/A", "none", "Not In Subdivision", "Metes and Bounds", "   "])
    def test_placeholders_are_none(self, value):
        assert hhc.extract_subdivision(_detail_prop([f"Subdivision: {value}"])) is None

    def test_absent_line_is_none(self):
        assert hhc.extract_subdivision(_detail_prop()) is None

    def test_source_neighborhood_is_not_used(self):
        assert hhc.extract_subdivision(_detail_prop(["Source Neighborhood: West"])) is None


class TestExtractSpecialConditions:
    def test_na_dropped(self):
        prop = _detail_prop(["Special Listing Conditions: Short Sale, Pre-Foreclosure, N/A"])
        assert hhc.extract_special_conditions(prop) == ["Short Sale", "Pre-Foreclosure"]

    def test_only_na_is_empty(self):
        assert hhc.extract_special_conditions(_detail_prop(["Special Listing Conditions: N/A"])) == []

    def test_absent_line_is_empty(self):
        assert hhc.extract_special_conditions(_detail_prop()) == []

    def test_reads_model_style_details(self):
        prop = SimpleNamespace(details=[SimpleNamespace(text=["Special Listing Conditions: Auction"])])
        assert hhc.extract_special_conditions(prop) == ["Auction"]
