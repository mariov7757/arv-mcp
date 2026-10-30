"""Tests for data/client.py — the Realtor.com transport over HomeHarvest
(HTTP mocked at requests.post)."""
from types import SimpleNamespace

import pytest
from homeharvest.core.scrapers.models import ListingType

from arv_mcp import ratelimit
from arv_mcp.data import client


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, bad_json=False):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": {}}
        self.headers = headers or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def http(monkeypatch):
    """Queue responses for requests.post; records each call's kwargs."""
    state = SimpleNamespace(responses=[], calls=[], sleeps=[])

    def post(url, **kw):
        state.calls.append(kw)
        r = state.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(client.requests, "post", post)
    monkeypatch.setattr(client.time, "sleep", lambda s: state.sleeps.append(s))
    monkeypatch.setattr(ratelimit.realtor_limiter, "acquire", lambda max_wait_s: None)
    return state


def _scraper():
    return client._Scraper(client._input(location="x", listing_type=None))


class TestGraphqlPost:
    def test_success_with_timeout(self, http):
        http.responses = [FakeResponse(payload={"data": {"ok": 1}})]
        assert _scraper()._graphql_post("query Q { a }", {"v": 1}, "Q") == {"data": {"ok": 1}}
        assert http.calls[0]["timeout"] == client.REQUEST_TIMEOUT_S

    @pytest.mark.parametrize("status", [403, 429])
    def test_retries_once_then_succeeds(self, http, status):
        http.responses = [FakeResponse(status, headers={"Retry-After": "3"}), FakeResponse(payload={"ok": 2})]
        assert _scraper()._graphql_post("q", {}, "Q") == {"ok": 2}
        assert http.sleeps == [3.0]

    def test_second_refusal_raises_and_is_recorded(self, http):
        http.responses = [FakeResponse(403), FakeResponse(403)]
        scraper = _scraper()
        with pytest.raises(client.UpstreamError, match="403"):
            scraper._graphql_post("q", {}, "Q")
        assert len(scraper.errors) == 1

    def test_network_error_retried_then_raised(self, http):
        err = client.requests.ConnectionError("down")
        http.responses = [err, err]
        with pytest.raises(client.UpstreamError, match="request failed"):
            _scraper()._graphql_post("q", {}, "Q")

    def test_other_http_error_not_retried(self, http):
        http.responses = [FakeResponse(500)]
        with pytest.raises(client.UpstreamError, match="500"):
            _scraper()._graphql_post("q", {}, "Q")
        assert len(http.calls) == 1

    def test_non_json(self, http):
        http.responses = [FakeResponse(bad_json=True)]
        with pytest.raises(client.UpstreamError, match="non-JSON"):
            _scraper()._graphql_post("q", {}, "Q")

    def test_rate_limit_propagates_without_a_request(self, http, monkeypatch):
        def limited(max_wait_s):
            raise ratelimit.RateLimited(12.0)
        monkeypatch.setattr(ratelimit.realtor_limiter, "acquire", limited)
        with pytest.raises(ratelimit.RateLimited):
            _scraper()._graphql_post("q", {}, "Q")
        assert http.calls == []

    @pytest.mark.parametrize("header,expected", [(None, 2.0), ("0", 1.0), ("99", client.MAX_RETRY_WAIT_S), ("x", 2.0)])
    def test_retry_after_bounds(self, header, expected):
        resp = FakeResponse(headers={} if header is None else {"Retry-After": header})
        assert client._retry_after(resp) == expected


class TestScraper:
    def test_fixed_centroid_replaces_geocoding(self):
        scraper = client._Scraper(client._input(location="x", listing_type=None), centroid=(33.4, -111.8))
        loc = scraper.handle_location()
        assert loc["area_type"] == "address"
        assert loc["centroid"] == {"lat": 33.4, "lon": -111.8}

    def test_input_defaults(self):
        si = client._input(location="x", listing_type=None)
        assert si.return_type.value == "pydantic"
        assert si.extra_property_data is False


class TestPublicCalls:
    def test_suggest_location_skips_homeharvest_retry_and_surfaces_errors(self, http):
        http.responses = [FakeResponse(403), FakeResponse(403)]
        with pytest.raises(client.UpstreamError):
            client.suggest_location("123 Main St, Mesa, AZ")
        assert len(http.calls) == 2  # our one retry only, not HomeHarvest's 3 attempts on top

    def test_suggest_location_parses_geocoder(self, http):
        http.responses = [FakeResponse(payload={"data": {"search_suggestions": {"geo_results": [{
            "text": "123 Main St, Mesa, AZ",
            "geo": {"area_type": "address", "mpr_id": "M1", "centroid": {"lat": 33.4, "lon": -111.8}},
        }]}}})]
        loc = client.suggest_location("123 Main St, Mesa, AZ")
        assert (loc["area_type"], loc["mpr_id"], loc["centroid"]["lat"]) == ("address", "M1", 33.4)

    def test_fetch_home_surfaces_errors_homeharvest_would_swallow(self, http):
        http.responses = [FakeResponse(429), FakeResponse(429)]
        with pytest.raises(client.UpstreamError, match="429"):
            client.fetch_home("M1")

    def test_fetch_home_not_found(self, http):
        http.responses = [FakeResponse(payload={"data": {"home": None}})]
        assert client.fetch_home("M1") is None

    def test_search_near_sends_coordinates_and_filters(self, http):
        http.responses = [FakeResponse(payload={"data": {"home_search": {"total": 0, "results": []}}})]
        assert client.search_near(33.4, -111.8, listing_type=ListingType.SOLD, radius_mi=1.5,
                                  property_types=["single_family"], sqft_min=1000, sqft_max=2000,
                                  past_days=180, sort_by="sold_date") == []
        sent = client.json.loads(http.calls[0]["data"])["variables"]
        assert sent["coordinates"] == [-111.8, 33.4]
        assert sent["radius"] == "1.5mi"

    def test_search_near_errors_propagate(self, http):
        http.responses = [FakeResponse(403), FakeResponse(403)]
        with pytest.raises(client.UpstreamError):
            client.search_near(33.4, -111.8, listing_type=ListingType.SOLD, radius_mi=1.5)
