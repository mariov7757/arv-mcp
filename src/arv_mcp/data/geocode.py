"""
geocode.py — US Census Bureau forward geocoder (address -> lat/lon).

Free and keyless. Used only when a subject address can't be verified against
a Realtor.com record, to still place it on the map. Street-range
interpolation, so house-level accuracy is approximate; it refuses addresses
that don't exist on a known street range (e.g. a made-up house number),
which is exactly the behaviour wanted here. See
https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.pdf
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from ..ratelimit import geocoder_limiter, max_wait_s

log = logging.getLogger(__name__)

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_BENCHMARK = "Public_AR_Current"
TIMEOUT_S = 10


def census_geocode(address: str) -> dict | None:
    """{"latitude", "longitude", "matched_address"} for the best Census match,
    or None when there's no match or the service fails (fails closed so the
    caller can try its next locator). RateLimited propagates."""
    if not address or not address.strip():
        return None
    geocoder_limiter.acquire(max_wait_s())
    qs = urllib.parse.urlencode({"address": address.strip(), "benchmark": CENSUS_BENCHMARK, "format": "json"})
    try:
        with urllib.request.urlopen(f"{CENSUS_URL}?{qs}", timeout=TIMEOUT_S) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.warning(f"Census geocoder failed for {address!r}: {e}")
        return None
    return parse_census_response(data)


def parse_census_response(data: dict) -> dict | None:
    try:
        matches = data["result"]["addressMatches"]
    except (KeyError, TypeError):
        return None
    if not matches:
        return None
    m = matches[0]
    try:
        return {
            "latitude": float(m["coordinates"]["y"]),
            "longitude": float(m["coordinates"]["x"]),
            "matched_address": m.get("matchedAddress"),
        }
    except (KeyError, TypeError, ValueError):
        return None
