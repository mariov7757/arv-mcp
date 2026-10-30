"""Tests for comps.py — comp fetching (around the subject's coordinates) and
normalization of HomeHarvest pydantic Property objects to comp dicts.

Test doubles build real homeharvest Property/Address/Description instances
rather than dicts, so they can't drift out of sync with the actual schema
comps.py reads from.
"""
from datetime import date, timedelta

import pytest
from homeharvest.core.scrapers.models import Address, Description, Property, PropertyType

from arv_mcp import comps


def _listing(**overrides):
    data = dict(
        address="123 Main St", city="Tucson", state="AZ", zip="85701",
        sqft=1500, beds=3, property_type="single family", latitude=32.2, longitude=-110.9,
    )
    data.update(overrides)
    return data


def _property(**overrides):
    address = overrides.pop("address", None) or Address(street="456 Oak Ave", city="Tucson", state="AZ")
    description = overrides.pop("description", None) or Description(
        style=PropertyType.SINGLE_FAMILY, baths_full=2, baths_half=1,
        year_built=1995, sqft=1600, beds=3, sold_price=250000,
    )
    defaults = dict(
        property_url="https://example.com/456-oak-ave", property_id="P1",
        address=address, description=description,
        tags=[], last_sold_date="2024-01-15", days_on_mls=30,
        prc_sqft=156, latitude=32.2, longitude=-110.9, neighborhoods=None,
    )
    defaults.update(overrides)
    return Property(**defaults)


class TestFetchComps:
    def test_no_coordinates_returns_empty_without_fetching(self, monkeypatch):
        monkeypatch.setattr(comps, "get_comps", lambda *a, **k: pytest.fail("should not fetch"))
        assert comps.fetch_comps(_listing(latitude=None)) == []

    def test_upstream_failure_propagates(self, monkeypatch):
        # A blocked or failed request must never read as "no comps nearby".
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(comps, "get_comps", boom)
        with pytest.raises(RuntimeError):
            comps.fetch_comps(_listing())

    def test_get_comps_returns_none_or_empty(self, monkeypatch):
        monkeypatch.setattr(comps, "get_comps", lambda *a, **k: None)
        assert comps.fetch_comps(_listing()) == []
        monkeypatch.setattr(comps, "get_comps", lambda *a, **k: [])
        assert comps.fetch_comps(_listing()) == []

    def test_get_comps_returns_normalized_rows(self, monkeypatch):
        monkeypatch.setattr(comps, "get_comps", lambda *a, **k: [_property()])
        result = comps.fetch_comps(_listing())
        assert len(result) == 1
        c = result[0]
        assert c["address"] == "456 Oak Ave, Tucson, AZ"
        assert c["sold_price"] == 250000
        assert c["baths"] == 2.5
        assert c["sqft"] == 1600
        assert c["_street_key"] == ("456 oak ave", None)

    def test_does_not_truncate_a_large_candidate_pool(self, monkeypatch):
        # comps.py deliberately hands every non-bad-data candidate through to
        # scoring.py rather than capping the pool itself -- scoring.py filters
        # later. Distinct addresses/prices so none trip the outlier/bulk drops.
        pool = [
            _property(
                property_id=f"P{i}",
                address=Address(street=f"{i} Elm St", city="Tucson", state="AZ"),
                prc_sqft=150.0 + i,
            )
            for i in range(15)
        ]
        monkeypatch.setattr(comps, "get_comps", lambda *a, **k: pool)
        assert len(comps.fetch_comps(_listing())) == 15

    def test_passes_coordinates_type_sqft_and_window(self, monkeypatch):
        captured = {}

        def fake_get_comps(lat, lon, property_types, sqft, radius_mi, days_back):
            captured.update(lat=lat, lon=lon, property_types=property_types, sqft=sqft,
                            radius_mi=radius_mi, days_back=days_back)
            return []

        monkeypatch.setattr(comps, "get_comps", fake_get_comps)
        comps.fetch_comps(_listing(property_type="condominium"), radius_mi=3.0, days_back=365)
        assert captured == dict(lat=32.2, lon=-110.9, property_types=["condos"], sqft=1500,
                                radius_mi=3.0, days_back=365)

    def test_default_window(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(comps, "get_comps",
                            lambda lat, lon, t, sqft, r, d: captured.update(r=r, d=d) or [])
        comps.fetch_comps(_listing())
        assert captured == {"r": 1.5, "d": 180}


class TestSplitSubject:
    def _comp(self, street, unit=None):
        return {"address": street, "_street_key": (street.lower(), unit)}

    def test_pulls_out_the_subjects_own_sale(self):
        own, other = self._comp("123 main st"), self._comp("456 oak ave")
        others, subject_sales = comps.split_subject([own, other], ("123 main st", None))
        assert others == [other]
        assert subject_sales == [own]

    def test_unit_subject_only_matches_its_own_unit(self):
        mine, sibling = self._comp("400 tower st", "601"), self._comp("400 tower st", "305")
        others, subject_sales = comps.split_subject([mine, sibling], ("400 tower st", "601"))
        assert subject_sales == [mine]
        assert others == [sibling]

    def test_blank_subject_key_keeps_everything(self):
        pool = [self._comp("123 main st")]
        assert comps.split_subject(pool, ("", None)) == (pool, [])


class TestResolvePropertyTypes:
    def test_known_type_mapped(self):
        assert comps._resolve_property_types({"property_type": "Townhouse"}) == ["townhomes"]

    def test_unknown_type_returns_none(self):
        assert comps._resolve_property_types({"property_type": "yurt"}) is None

    def test_missing_type_returns_none(self):
        assert comps._resolve_property_types({}) is None

    def test_case_and_whitespace_normalized(self):
        assert comps._resolve_property_types({"property_type": "  Multi Family  "}) == ["multi_family"]


class TestDaysAgo:
    def test_none_returns_zero(self):
        assert comps._days_ago(None) == 0

    def test_nan_returns_zero(self):
        assert comps._days_ago(float("nan")) == 0

    def test_string_date(self):
        target = date.today() - timedelta(days=30)
        assert comps._days_ago(target.strftime("%Y-%m-%d")) == 30

    def test_datetime_like_with_date_method(self):
        import datetime as dt
        target = dt.datetime.combine(date.today() - timedelta(days=5), dt.time())
        assert comps._days_ago(target) == 5

    def test_unparseable_value_returns_zero(self):
        assert comps._days_ago("not-a-date") == 0


class TestNormalizeComps:
    def test_falls_back_to_last_sold_price(self):
        prop = _property(
            description=Description(style=PropertyType.SINGLE_FAMILY, baths_full=2,
                                     year_built=2001, sqft=1400, beds=3, sold_price=None),
            last_sold_price=300000,
        )
        result = comps._normalize_comps([prop])
        assert result[0]["sold_price"] == 300000

    def test_address_skips_missing_parts(self):
        prop = _property(address=Address(street="1 St", city=None, state="AZ"))
        result = comps._normalize_comps([prop])
        assert result[0]["address"] == "1 St, AZ"

    def test_unit_levels_garage_and_conditions_from_details(self):
        # Real Property model: details dicts coerce to HomeDetails, so this
        # exercises the live shape extract_* read from.
        prop = _property(
            description=Description(style=PropertyType.CONDOS, sqft=1100, beds=2,
                                     sold_price=200000, stories=4, garage=None),
            details=[{"category": "Building", "text": [
                "Levels: 1", "Building Total Stories: 4", "Garage Spaces: 1",
                "Special Listing Conditions: Lender Owned/REO, N/A", "Subdivision: KNOELL MESA UNIT 2",
            ]}],
        )
        c = comps._normalize_comps([prop])[0]
        assert c["subdivision"] == "KNOELL MESA UNIT 2"
        assert c["stories"] == 1
        assert c["garage"] == 1.0
        assert c["special_listing_conditions"] == ["Lender Owned/REO"]

    def test_carport_only_comp_is_zero_garage_and_unknown_stories_is_zero(self):
        prop = _property(tags=["carport"])
        c = comps._normalize_comps([prop])[0]
        assert c["garage"] == 0.0
        assert c["stories"] == 0
        assert c["special_listing_conditions"] == []
        assert c["subdivision"] is None


# ── fetch_active_pending_comps / _normalize_active_pending ───────────────────

def _for_sale_property(**overrides):
    """Active/pending Property double -- no sold_price/last_sold_date (never
    happened), list_price/list_date/status instead."""
    address = overrides.pop("address", None) or Address(street="789 Pine Rd", city="Tucson", state="AZ")
    description = overrides.pop("description", None) or Description(
        style=PropertyType.SINGLE_FAMILY, baths_full=2, baths_half=0,
        year_built=2005, sqft=1700, beds=3,
    )
    defaults = dict(
        property_url="https://example.com/789-pine-rd", property_id="P2",
        address=address, description=description,
        tags=[], status="FOR_SALE", list_price=310000, list_date="2025-06-01",
        pending_date=None, days_on_mls=14, prc_sqft=182, latitude=32.25,
        longitude=-110.95, neighborhoods=None,
    )
    defaults.update(overrides)
    return Property(**defaults)


class TestFetchActivePendingComps:
    def test_no_coordinates_returns_empty_without_fetching(self, monkeypatch):
        monkeypatch.setattr(comps, "get_active_pending", lambda *a, **k: pytest.fail("should not fetch"))
        assert comps.fetch_active_pending_comps(_listing(longitude=None)) == []

    def test_upstream_failure_propagates(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(comps, "get_active_pending", boom)
        with pytest.raises(RuntimeError):
            comps.fetch_active_pending_comps(_listing())

    def test_get_active_pending_returns_none_or_empty(self, monkeypatch):
        monkeypatch.setattr(comps, "get_active_pending", lambda *a, **k: None)
        assert comps.fetch_active_pending_comps(_listing()) == []
        monkeypatch.setattr(comps, "get_active_pending", lambda *a, **k: [])
        assert comps.fetch_active_pending_comps(_listing()) == []

    def test_returns_normalized_rows(self, monkeypatch):
        monkeypatch.setattr(comps, "get_active_pending", lambda *a, **k: [_for_sale_property()])
        result = comps.fetch_active_pending_comps(_listing())
        assert len(result) == 1
        c = result[0]
        assert c["address"] == "789 Pine Rd, Tucson, AZ"
        assert c["listing_status"] == "ACTIVE"
        assert c["list_price"] == 310000
        assert c["sold_price"] is None
        assert c["sqft"] == 1700

    def test_passes_coordinates_type_sqft_and_radius(self, monkeypatch):
        captured = {}

        def fake(lat, lon, property_types, sqft, radius_mi):
            captured.update(lat=lat, lon=lon, property_types=property_types, sqft=sqft, radius_mi=radius_mi)
            return []

        monkeypatch.setattr(comps, "get_active_pending", fake)
        comps.fetch_active_pending_comps(_listing(property_type="condominium"), radius_mi=2.0)
        assert captured == dict(lat=32.2, lon=-110.9, property_types=["condos"], sqft=1500, radius_mi=2.0)

    def test_drops_the_subject_s_own_listing(self, monkeypatch):
        # The subject is often for sale itself, and the search is centered on
        # it -- without filtering it would comp itself (see _subject_street_key).
        subject_as_result = _for_sale_property(address=Address(street="123 Main St", city="Tucson", state="AZ"))
        other = _for_sale_property()  # default "789 Pine Rd" -- a real, distinct comp
        monkeypatch.setattr(comps, "get_active_pending", lambda *a, **k: [subject_as_result, other])
        result = comps.fetch_active_pending_comps(_listing(address="123 Main St"))
        assert len(result) == 1
        assert result[0]["address"] == "789 Pine Rd, Tucson, AZ"

    def test_unit_keyword_on_result_still_matches_subject_unit(self, monkeypatch):
        # HomeHarvest stores units as "Unit 601"; the subject's unit is "601".
        mine = _for_sale_property(address=Address(street="400 Tower St", unit="Unit 601", city="Phoenix", state="AZ"))
        sibling = _for_sale_property(address=Address(street="400 Tower St", unit="Unit 305", city="Phoenix", state="AZ"))
        monkeypatch.setattr(comps, "get_active_pending", lambda *a, **k: [mine, sibling])
        result = comps.fetch_active_pending_comps(_listing(address="400 Tower St", unit="601"))
        assert [c["address"] for c in result] == ["400 Tower St Unit 305, Phoenix, AZ"]


class TestSubjectStreetKey:
    def test_no_address_returns_blank(self):
        assert comps._subject_street_key({}) == ("", None)

    def test_normalizes_suffix_and_case(self):
        base, unit = comps._subject_street_key({"address": "123 Main Street"})
        assert base == "123 main st"
        assert unit is None

    def test_unit_baked_into_address_is_split_out(self):
        base, unit = comps._subject_street_key({"address": "400 Tower St 601"})
        assert base == "400 tower st"
        assert unit == "601"


class TestListingStatus:
    def test_pending_status(self):
        assert comps._listing_status("PENDING") == "PENDING"

    def test_contingent_grouped_under_pending(self):
        assert comps._listing_status("CONTINGENT") == "PENDING"

    def test_for_sale_status(self):
        assert comps._listing_status("FOR_SALE") == "ACTIVE"

    def test_blank_defaults_to_active(self):
        assert comps._listing_status(None) == "ACTIVE"


class TestNormalizeActivePending:
    def test_active_status_and_list_price(self):
        prop = _for_sale_property(status="FOR_SALE", list_price=250000)
        result = comps._normalize_active_pending([prop])
        assert result[0]["listing_status"] == "ACTIVE"
        assert result[0]["list_price"] == 250000
        assert result[0]["sold_price"] is None

    def test_pending_uses_pending_date_for_recency(self):
        from datetime import date, timedelta
        recent = date.today() - timedelta(days=5)
        stale = date.today() - timedelta(days=200)
        prop = _for_sale_property(
            status="PENDING", list_date=stale.isoformat(), pending_date=recent.isoformat(),
        )
        result = comps._normalize_active_pending([prop])
        assert result[0]["listing_status"] == "PENDING"
        assert result[0]["days_ago"] == 5

    def test_active_uses_list_date_for_recency(self):
        from datetime import date, timedelta
        recent = date.today() - timedelta(days=10)
        prop = _for_sale_property(status="FOR_SALE", list_date=recent.isoformat(), pending_date=None)
        result = comps._normalize_active_pending([prop])
        assert result[0]["days_ago"] == 10

    def test_price_sqft_falls_back_to_list_price_over_sqft(self):
        prop = _for_sale_property(
            prc_sqft=None,
            description=Description(style=PropertyType.SINGLE_FAMILY, sqft=1000, beds=3),
            list_price=200000,
        )
        result = comps._normalize_active_pending([prop])
        assert result[0]["price_sqft"] == 200.0

    def test_unit_levels_and_conditions_from_details(self):
        prop = _for_sale_property(details=[{"text": [
            "Levels: 2", "Special Listing Conditions: Pre-Foreclosure", "Subdivision: Seville",
        ]}])
        c = comps._normalize_active_pending([prop])[0]
        assert c["subdivision"] == "Seville"
        assert c["stories"] == 2
        assert c["special_listing_conditions"] == ["Pre-Foreclosure"]

    def test_drops_result_matching_subject_key(self):
        prop = _for_sale_property(address=Address(street="123 Main St", city="Tucson", state="AZ"))
        subject_key = comps._subject_street_key({"address": "123 Main St"})
        result = comps._normalize_active_pending([prop], subject_key)
        assert result == []

    def test_keeps_result_with_different_unit_same_street(self):
        # Same building, different unit -- a genuinely distinct property, not
        # the subject itself, so it must NOT be dropped.
        prop = _for_sale_property(address=Address(street="123 Main St", unit="202", city="Tucson", state="AZ"))
        subject_key = comps._subject_street_key({"address": "123 Main St 101"})
        result = comps._normalize_active_pending([prop], subject_key)
        assert len(result) == 1

    def test_no_subject_key_keeps_everything(self):
        prop = _for_sale_property(address=Address(street="123 Main St", city="Tucson", state="AZ"))
        result = comps._normalize_active_pending([prop])
        assert len(result) == 1


# ── build_location ────────────────────────────────────────────────────────────

class TestBuildLocation:
    def test_full(self):
        listing = {"address": "123 Main St", "city": "Tucson", "state": "AZ", "zip": "85701"}
        assert comps.build_location(listing) == "123 Main St, Tucson, AZ, 85701"

    def test_missing_fields(self):
        listing = {"address": "123 Main St", "city": "", "state": "AZ", "zip": None}
        assert comps.build_location(listing) == "123 Main St, AZ"

    def test_all_missing(self):
        assert comps.build_location({}) == ""

    def test_address_only(self):
        assert comps.build_location({"address": "123 Main St"}) == "123 Main St"
