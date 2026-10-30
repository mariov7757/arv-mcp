"""Tests for service.py — subject resolution, comps, estimate shaping and
errors. The data layer (resolve_record, comp fetches, geocoder) is mocked."""
import pytest

from arv_mcp import service
from arv_mcp.data.client import UpstreamError
from arv_mcp.ratelimit import RateLimited

LAT, LON = 33.39, -111.78


def _record(**kw):
    base = dict(
        address="1234 Main St", unit=None, city="Mesa", state="AZ", zip="85204",
        sqft=2000, beds=4, baths=2.0, lot_sqft=8000, year_built=1980, garage=2.0, stories=1,
        has_pool=False, has_basement=False, special_listing_conditions=[], subdivision="KNOELL MESA UNIT 1",
        property_type="single_family", listed_price=432500, last_sold_price=300000,
        last_sold_date="2021-08-11", latitude=LAT, longitude=LON, description="", property_url="https://x/1",
        mls_status="SOLD", active=False, on_market=False, address_fuzzy_match=False,
    )
    base.update(kw)
    return base


def _comp(i, price_sqft=250.0, **kw):
    street = f"{100 + i} E Test St"
    base = dict(
        address=f"{street}, Mesa, AZ", _street_key=(street.lower(), None), sold_price=int(price_sqft * 2000),
        price_sqft=price_sqft, sqft=2000, beds=4, baths=2.0, year_built=1981, lot_sqft=8000, days_ago=30,
        type="single_family", latitude=LAT + 0.001 * (i + 1), longitude=LON, garage=2.0, stories=1,
        has_pool=False, has_basement=False, description_text="", special_listing_conditions=[],
        subdivision=None, property_url=None, days_on_market=20,
    )
    base.update(kw)
    return base


def _listing(i, status="ACTIVE", price_sqft=260.0):
    return dict(address=f"{500 + i} E Listed Ave, Mesa, AZ", listing_status=status, list_price=int(price_sqft * 2000),
                price_sqft=price_sqft, sqft=2000, beds=4, baths=2.0, year_built=1985, days_on_market=10,
                latitude=LAT + 0.002 * (i + 1), longitude=LON, special_listing_conditions=[])


@pytest.fixture
def data(monkeypatch):
    """Configurable fake data layer."""
    class D:
        record = _record()
        suggestion = {"area_type": "address", "text": "1234 Main St, Mesa, AZ",
                      "centroid": {"lat": LAT, "lon": LON}, "mpr_id": "M1"}
        census = None
        sold = [_comp(i, 245 + i) for i in range(8)]
        market = [_listing(0), _listing(1, "PENDING")]
        calls: list = []

    d = D()
    monkeypatch.setattr(service.hh, "resolve_record",
                        lambda address: d.calls.append(("resolve", address)) or (d.record, d.suggestion))
    monkeypatch.setattr(service, "census_geocode", lambda address: d.calls.append(("census", address)) or d.census)

    def sold(subject, radius_mi, days_back):
        d.calls.append(("sold", subject["latitude"], subject["longitude"], radius_mi, days_back))
        return d.sold

    def market(subject, radius_mi):
        d.calls.append(("market", radius_mi))
        return d.market

    monkeypatch.setattr(service, "fetch_comps", sold)
    monkeypatch.setattr(service, "fetch_active_pending_comps", market)
    return d


def _err(fn, *a, **kw) -> service.ArvError:
    with pytest.raises(service.ArvError) as exc:
        fn(*a, **kw)
    return exc.value


# ── Verified subject ──────────────────────────────────────────────────────────

class TestVerifiedSubject:
    def test_estimate_shape(self, data):
        r = service.get_arv("1234 Main St, Mesa, AZ 85204")
        s, e = r["subject"], r["estimate"]
        assert s["subject_source"] == "record" and s["location_source"] == "record"
        assert s["address"] == "1234 Main St, Mesa, AZ 85204"
        assert e["method"] == "weighted_median" and e["arv_fallback"] is False
        assert e["arv"] == round(e["price_sqft"] * 2000, -3) or abs(e["arv"] - e["price_sqft"] * 2000) < 2000
        assert e["window"] == {"radius_mi": 1.5, "days_back": 180}
        assert r["confidence"]["tier"] in (4, 5)
        assert r["pool_stats"]["count"] == 8
        assert len(r["eligible_comps"]) == 8
        assert r["market_context"]["active_count"] == 1 and r["market_context"]["pending_count"] == 1

    def test_renovated_range_from_top_of_pool(self, data):
        e = service.get_arv("1234 Main St, Mesa, AZ")["estimate"]
        rr = e["renovated_range"]
        assert rr["low"] >= e["arv"] and rr["high"] >= rr["low"]
        assert rr["high_price_sqft"] <= 252

    def test_no_renovated_range_below_five_comps(self, data):
        data.sold = [_comp(i, 250) for i in range(3)]
        r = service.get_arv("1234 Main St, Mesa, AZ")
        assert r["estimate"]["renovated_range"] is None
        assert any("renovated" in g for g in r["guidance"])

    def test_off_market_list_price_is_hidden(self, data):
        assert service.get_arv("1234 Main St, Mesa, AZ")["subject"]["list_price"] is None

    def test_on_market_list_price_is_shown(self, data):
        data.record = _record(on_market=True, mls_status="FOR_SALE", listed_price=399000)
        assert service.get_arv("1234 Main St, Mesa, AZ")["subject"]["list_price"] == 399000

    def test_overrides_apply_and_are_listed(self, data):
        r = service.get_arv("1234 Main St, Mesa, AZ", has_pool=True, sqft=2100, property_type="Single Family")
        s = r["subject"]
        assert s["has_pool"] is True and s["sqft"] == 2100 and s["property_type"] == "single_family"
        assert s["overridden"] == ["has_pool", "property_type", "sqft"]

    def test_comps_searched_around_record_coordinates(self, data):
        service.get_arv("1234 Main St, Mesa, AZ")
        assert ("sold", LAT, LON, 1.5, 180) in data.calls

    def test_record_without_coordinates_uses_geocoder_point(self, data):
        data.record = _record(latitude=None, longitude=None)
        data.suggestion = dict(data.suggestion, centroid={"lat": 33.0, "lon": -111.0})
        r = service.get_arv("1234 Main St, Mesa, AZ")
        assert r["subject"]["location_source"] == "realtor_geocoder"
        assert ("sold", 33.0, -111.0, 1.5, 180) in data.calls

    def test_record_missing_sqft(self, data):
        data.record = _record(sqft=None)
        e = _err(service.get_arv, "1234 Main St, Mesa, AZ")
        assert e.code == "MISSING_FACTS" and e.details["needs"] == ["sqft"]
        assert service.get_arv("1234 Main St, Mesa, AZ", sqft=1900)["subject"]["sqft"] == 1900

    def test_fuzzy_match_flagged(self, data):
        data.record = _record(address_fuzzy_match=True)
        assert any("fuzzy" in g for g in service.get_arv("1234 Main, Mesa, AZ")["guidance"])


# ── Unverified subject ────────────────────────────────────────────────────────

class TestUnverifiedSubject:
    @pytest.fixture(autouse=True)
    def _no_record(self, data):
        data.record = None

    def test_not_found_when_nothing_locates(self, data):
        data.suggestion = {"area_type": "city", "text": "Mesa, AZ", "centroid": {"lat": 1, "lon": 1}}
        e = _err(service.get_arv, "99999 E Main St, Mesa, AZ 85204")
        assert e.code == "ADDRESS_NOT_FOUND"
        assert not any(c[0] == "sold" for c in data.calls)

    def test_located_but_needs_facts(self, data):
        data.census = {"latitude": 33.41, "longitude": -111.77, "matched_address": "700 PARK WAY"}
        e = _err(service.get_arv, "700 Park Way Lot 90, Mesa, AZ 85213", beds=2)
        assert e.code == "ADDRESS_NOT_VERIFIED"
        assert e.details["needs"] == ["sqft", "property_type"]
        assert "beds" not in e.details["recommended"]
        assert e.details["located"]["source"] == "census"

    def test_user_supplied_subject_from_census_location(self, data):
        data.census = {"latitude": 33.41, "longitude": -111.77, "matched_address": "700 PARK WAY"}
        r = service.get_arv("700 Park Way Lot 90, Mesa, AZ 85213", sqft=1000, property_type="single family")
        s = r["subject"]
        assert (s["subject_source"], s["location_source"]) == ("user_supplied", "census")
        assert s["latitude"] == 33.41 and s["sqft"] == 1000
        assert "user-supplied" in r["guidance"][0]
        assert ("sold", 33.41, -111.77, 1.5, 180) in data.calls

    def test_caller_coordinates_win_and_skip_census(self, data):
        r = service.get_arv("Somewhere", sqft=1000, property_type="condo", latitude=33.5, longitude=-112.0)
        assert r["subject"]["location_source"] == "caller"
        assert not any(c[0] == "census" for c in data.calls)

    def test_realtor_building_point_only_on_same_street(self, data):
        data.suggestion = {"area_type": "address", "text": "500 Park Ln Unit 439, Mesa, AZ",
                           "centroid": {"lat": 33.3, "lon": -111.6}}
        e = _err(service.get_arv, "500 Park Ln Unit 1433, Mesa, AZ 85209")
        assert e.code == "ADDRESS_NOT_VERIFIED" and e.details["located"]["source"] == "realtor_building"
        data.suggestion = dict(data.suggestion, text="2000 W Elm St, Mesa, AZ")
        service.clear_caches()
        assert _err(service.get_arv, "500 Park Ln Unit 1433, Mesa, AZ 85209").code == "ADDRESS_NOT_FOUND"

    def test_lat_without_lon_is_invalid(self, data):
        assert _err(service.get_arv, "x", latitude=33.0).code == "INVALID_INPUT"


# ── Comps, estimate quality, guidance ─────────────────────────────────────────

class TestEstimate:
    def test_subjects_own_sale_excluded_and_reported(self, data):
        own = _comp(0, 216.0, address="1234 Main St, Mesa, AZ", _street_key=("1234 main st", None),
                    days_ago=3, sold_price=432500)
        data.sold = [own] + [_comp(i, 260) for i in range(1, 7)]
        r = service.get_arv("1234 Main St, Mesa, AZ")
        assert all("1234 Main" not in c["address"] for c in r["eligible_comps"])
        assert r["subject"]["recent_sale_in_window"] == {"sold_price": 432500, "price_sqft": 216, "sold_days_ago": 3}
        assert any("subject itself sold 3 days ago" in g for g in r["guidance"])

    def test_no_comps(self, data):
        data.sold = []
        e = _err(service.get_arv, "1234 Main St, Mesa, AZ")
        assert e.code == "NO_COMPS" and "subject" in e.details

    def test_fallback_when_nothing_eligible(self, data):
        data.sold = [_comp(i, 250, type="condos") for i in range(4)]
        r = service.get_arv("1234 Main St, Mesa, AZ")
        assert r["estimate"]["arv_fallback"] is True and r["estimate"]["method"] == "area_anchor_fallback"
        assert r["estimate"]["renovated_range"] is None
        assert r["guidance"][0].startswith("FALLBACK ESTIMATE")
        assert r["excluded_count"] == 4 and r["excluded_comps"][0]["reason"]

    def test_distressed_comps_flagged(self, data):
        data.sold = [_comp(i, 250) for i in range(5)] + [
            _comp(5, 215, special_listing_conditions=["Lender Owned/REO"]),
            _comp(6, 216, description_text="Investor special, sold as-is"),
        ]
        r = service.get_arv("1234 Main St, Mesa, AZ")
        flagged = {c["address"]: c["distress_reasons"] for c in r["eligible_comps"] if c["distressed"]}
        assert any(reasons == ["Lender Owned/REO"] for reasons in flagged.values())
        assert any("distress keyword" in reasons[0] for reasons in flagged.values())
        assert r["pool_stats"]["distressed_count"] == 2
        assert r["pool_stats"]["weighted_median_price_sqft_excluding_distressed"] == 250
        assert any("distressed sales" in g for g in r["guidance"])

    def test_owner_agent_condition_is_not_distress(self, data):
        data.sold = [_comp(i, 250, special_listing_conditions=["Owner/Agent"]) for i in range(5)]
        r = service.get_arv("1234 Main St, Mesa, AZ")
        assert r["pool_stats"]["distressed_count"] == 0

    def test_unknown_comp_facts_are_null_not_zero(self, data):
        data.sold = [_comp(i, 250, stories=0, lot_sqft=0, beds=0) for i in range(5)]
        c = service.get_arv("1234 Main St, Mesa, AZ")["eligible_comps"][0]
        assert (c["stories"], c["lot_sqft"], c["beds"]) == (None, None, None)

    def test_factors_list_only_what_cost_points(self, data):
        data.sold = [_comp(i, 250) for i in range(4)] + [_comp(4, 251, sqft=2300, has_pool=True)]
        comps = service.get_arv("1234 Main St, Mesa, AZ")["eligible_comps"]
        odd = next(c for c in comps if c["sqft"] == 2300)
        assert any(f.startswith("Sqft:") for f in odd["factors"])
        assert any(f.startswith("Pool:") for f in odd["factors"])
        assert not any(f.startswith("Year built") for f in odd["factors"])

    def test_max_comps_and_market_context_toggle(self, data):
        r = service.get_arv("1234 Main St, Mesa, AZ", max_comps=3, include_market_context=False)
        assert len(r["eligible_comps"]) == 3
        assert "market_context" not in r
        assert not any(c[0] == "market" for c in data.calls)


class TestPropertyTypes:
    def test_land_refused(self, data):
        data.record = _record(property_type="land")
        e = _err(service.get_arv, "1234 Main St, Mesa, AZ")
        assert e.code == "UNSUPPORTED_PROPERTY_TYPE"
        assert not any(c[0] == "sold" for c in data.calls)

    @pytest.mark.parametrize("ptype,phrase", [("mobile", "park"), ("duplex_triplex", "per unit"),
                                              ("multi_family", "per unit"), ("farm", "acreage")])
    def test_warnings(self, data, ptype, phrase):
        data.record = _record(property_type=ptype)
        data.sold = [_comp(i, 250, type=ptype) for i in range(5)]
        assert any(phrase in g for g in service.get_arv("1234 Main St, Mesa, AZ")["guidance"])

    def test_unknown_type(self, data):
        data.record = _record(property_type=None)
        assert any("type is unknown" in g for g in service.get_arv("1234 Main St, Mesa, AZ")["guidance"])

    def test_subject_reo_flagged(self, data):
        data.record = _record(special_listing_conditions=["Lender Owned/REO"])
        assert any("Subject itself is listed as Lender Owned/REO" in g
                   for g in service.get_arv("1234 Main St, Mesa, AZ")["guidance"])


# ── Window, cache, errors ─────────────────────────────────────────────────────

class TestWindow:
    def test_custom_window_reaches_fetch_and_eligibility(self, data):
        data.sold = [_comp(i, 250) for i in range(5)] + [_comp(9, 251, latitude=LAT + 0.03)]  # ~2.1 mi
        narrow = service.get_arv("1234 Main St, Mesa, AZ")
        wide = service.get_arv("1234 Main St, Mesa, AZ", radius_mi=3, days_back=365)
        assert narrow["pool_stats"]["count"] == 5 and wide["pool_stats"]["count"] == 6
        assert ("sold", LAT, LON, 3.0, 365) in data.calls
        assert any("Searched 3 mi / 365 days" in g for g in wide["guidance"])

    def test_wider_days_back_admits_older_sales(self, data):
        data.sold = [_comp(i, 250) for i in range(5)] + [_comp(9, 251, days_ago=300)]
        assert service.get_arv("1234 Main St, Mesa, AZ")["pool_stats"]["count"] == 5
        assert service.get_arv("1234 Main St, Mesa, AZ", days_back=365)["pool_stats"]["count"] == 6

    def test_out_of_range_window_is_clamped_and_reported(self, data):
        r = service.get_arv("1234 Main St, Mesa, AZ", radius_mi=50, days_back=5)
        assert r["estimate"]["window"] == {"radius_mi": 5.0, "days_back": 30}
        assert any("adjusted" in g for g in r["guidance"])


class TestCache:
    def test_what_if_reuses_record_and_comps(self, data):
        service.get_arv("1234 Main St, Mesa, AZ")
        n = len(data.calls)
        service.get_arv("1234  Main St, MESA, AZ", has_pool=True)
        assert len(data.calls) == n

    def test_different_window_refetches_comps_only(self, data):
        service.get_arv("1234 Main St, Mesa, AZ")
        service.get_arv("1234 Main St, Mesa, AZ", radius_mi=2)
        assert sum(1 for c in data.calls if c[0] == "resolve") == 1
        assert sum(1 for c in data.calls if c[0] == "sold") == 2

    def test_ttl_cache_expires(self):
        now = [0.0]
        cache = service._TTLCache(ttl_s=10, max_entries=2, clock=lambda: now[0])
        calls = []
        assert cache.get_or_set("a", lambda: calls.append(1) or 1) == 1
        assert cache.get_or_set("a", lambda: calls.append(1) or 2) == 1
        now[0] = 11
        assert cache.get_or_set("a", lambda: calls.append(1) or 3) == 3
        cache.get_or_set("b", lambda: 0)
        cache.get_or_set("c", lambda: 0)  # evicts the oldest
        assert len(cache._data) == 2


class TestErrors:
    def test_blank_address(self):
        assert _err(service.get_arv, "  ").code == "INVALID_INPUT"
        assert _err(service.lookup_property, "").code == "INVALID_INPUT"

    @pytest.mark.parametrize("kw", [{"sqft": 0}, {"sqft": -5}, {"beds": -1}, {"sqft": "abc"}])
    def test_bad_facts(self, data, kw):
        assert _err(service.get_arv, "1234 Main St, Mesa, AZ", **kw).code == "INVALID_INPUT"

    def test_upstream_and_rate_limit_mapped(self, data, monkeypatch):
        def blocked(address):
            raise UpstreamError("Realtor.com returned HTTP 403 (blocked).")
        monkeypatch.setattr(service.hh, "resolve_record", blocked)
        assert _err(service.get_arv, "1234 Main St, Mesa, AZ").code == "UPSTREAM_ERROR"

        def limited(address):
            raise RateLimited(12.5)
        monkeypatch.setattr(service.hh, "resolve_record", limited)
        e = _err(service.get_arv, "1234 Main St, Mesa, AZ")
        assert e.code == "RATE_LIMITED" and e.details["retry_after_s"] == 12.5

    def test_error_dict(self):
        assert service.ArvError("X", "msg", a=1).to_dict() == {"code": "X", "message": "msg", "a": 1}


class TestLookupProperty:
    def test_found(self, data):
        s = service.lookup_property("1234 Main St, Mesa, AZ")["subject"]
        assert s["sqft"] == 2000 and s["last_sold_price"] == 300000 and s["subdivision"] == "KNOELL MESA UNIT 1"

    def test_not_verified_vs_not_found(self, data):
        data.record = None
        data.suggestion = {"area_type": "address", "text": "1234 Main St Unit 2, Mesa, AZ"}
        e = _err(service.lookup_property, "1234 Main St Unit 9, Mesa, AZ")
        assert e.code == "ADDRESS_NOT_VERIFIED" and e.details["closest_match"].startswith("1234 Main")
        data.suggestion = {"area_type": "city", "text": "Mesa, AZ"}
        assert _err(service.lookup_property, "1234 Main St, Mesa, AZ").code == "ADDRESS_NOT_FOUND"

    def test_upstream_mapped(self, data, monkeypatch):
        def blocked(address):
            raise UpstreamError("down")
        monkeypatch.setattr(service.hh, "resolve_record", blocked)
        assert _err(service.lookup_property, "1234 Main St, Mesa, AZ").code == "UPSTREAM_ERROR"
