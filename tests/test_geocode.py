"""Tests for data/geocode.py — Census forward geocoder (HTTP mocked)."""
import io
import json

import pytest

from arv_mcp import ratelimit
from arv_mcp.data import geocode

MATCH = {"result": {"addressMatches": [{
    "matchedAddress": "700 PARK WAY, MESA, AZ, 85213",
    "coordinates": {"x": -111.77, "y": 33.41},
}]}}


@pytest.fixture(autouse=True)
def _free_limiter(monkeypatch):
    monkeypatch.setattr(ratelimit.geocoder_limiter, "acquire", lambda max_wait_s: None)


def _respond(monkeypatch, payload=None, exc=None):
    seen = {}

    def urlopen(url, timeout):
        seen.update(url=url, timeout=timeout)
        if exc:
            raise exc
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(geocode.urllib.request, "urlopen", urlopen)
    return seen


def test_match(monkeypatch):
    seen = _respond(monkeypatch, MATCH)
    assert geocode.census_geocode("700 Park Way Lot 90, Mesa, AZ 85213") == {
        "latitude": 33.41, "longitude": -111.77, "matched_address": "700 PARK WAY, MESA, AZ, 85213"}
    assert "benchmark=Public_AR_Current" in seen["url"]
    assert seen["timeout"] == geocode.TIMEOUT_S


def test_no_match_is_none(monkeypatch):
    _respond(monkeypatch, {"result": {"addressMatches": []}})
    assert geocode.census_geocode("99999 E Main St, Mesa, AZ") is None


def test_service_failure_fails_closed(monkeypatch):
    _respond(monkeypatch, exc=geocode.urllib.error.URLError("down"))
    assert geocode.census_geocode("1 Main St") is None


def test_blank_address_makes_no_request(monkeypatch):
    seen = _respond(monkeypatch, MATCH)
    assert geocode.census_geocode("  ") is None
    assert seen == {}


@pytest.mark.parametrize("payload", [{}, {"result": None}, {"result": {"addressMatches": [{"coordinates": {}}]}}])
def test_malformed_responses(payload):
    assert geocode.parse_census_response(payload) is None


def test_rate_limit_propagates(monkeypatch):
    def limited(max_wait_s):
        raise ratelimit.RateLimited(5)
    monkeypatch.setattr(ratelimit.geocoder_limiter, "acquire", limited)
    with pytest.raises(ratelimit.RateLimited):
        geocode.census_geocode("1 Main St")
