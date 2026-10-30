"""Live smoke tests against Realtor.com (via HomeHarvest) and the Census
geocoder. Deselected by default. Real addresses are supplied through
environment variables rather than stored in the repo:

    ARV_MCP_LIVE_ADDRESS=...            # a home with a Realtor.com record and nearby sales
    ARV_MCP_LIVE_UNVERIFIABLE=...       # optional: a unit/lot address with no record of its own
    pytest -m live

They guard the HomeHarvest internals data/client.py depends on (single-home
lookup, coordinate-centered search) and the end-to-end get_arv paths. Data
changes daily, so assertions are about shape and behaviour, not values.
"""
import os

import pytest
from homeharvest.core.scrapers.models import ListingType

from arv_mcp import service
from arv_mcp.data import client, homeharvest
from arv_mcp.data.geocode import census_geocode

pytestmark = pytest.mark.live

LIVE_ADDRESS = os.environ.get("ARV_MCP_LIVE_ADDRESS", "").strip()
UNVERIFIABLE_ADDRESS = os.environ.get("ARV_MCP_LIVE_UNVERIFIABLE", "").strip()
# Fake on purpose: no house exists at this number, so every locator must refuse it.
FAKE_ADDRESS = "99999 E Main St, Mesa, AZ 85204"

needs_address = pytest.mark.skipif(not LIVE_ADDRESS, reason="set ARV_MCP_LIVE_ADDRESS to a real address")


@pytest.fixture(scope="module")
def located():
    """The live address's Realtor.com geocoder result."""
    loc = client.suggest_location(LIVE_ADDRESS)
    assert loc and loc["area_type"] == "address" and loc["mpr_id"], loc
    return loc


@needs_address
def test_geocoder_lookup_and_home_fetch(located):
    home = client.fetch_home(located["mpr_id"])
    matched, _fuzzy = homeharvest.match_property(LIVE_ADDRESS, [home])
    assert matched is home


@needs_address
def test_coordinate_search_respects_radius_and_days(located):
    c = located["centroid"]
    homes = client.search_near(c["lat"], c["lon"], listing_type=ListingType.SOLD, radius_mi=1.0,
                               past_days=90, sort_by="sold_date", limit=50)
    assert homes
    assert all(h.latitude is not None for h in homes)


@needs_address
def test_census_geocoder(located):
    hit = census_geocode(LIVE_ADDRESS)
    assert hit and abs(hit["latitude"] - located["centroid"]["lat"]) < 0.01


def test_census_geocoder_refuses_fake_address():
    assert census_geocode(FAKE_ADDRESS) is None


@needs_address
def test_get_arv_verified_subject():
    r = service.get_arv(LIVE_ADDRESS)
    assert r["subject"]["subject_source"] == "record"
    assert r["estimate"]["arv"] > 0
    assert r["eligible_comps"]
    street = LIVE_ADDRESS.split(",")[0].lower()
    assert all(street not in c["address"].lower() for c in r["eligible_comps"])


@pytest.mark.skipif(not UNVERIFIABLE_ADDRESS, reason="set ARV_MCP_LIVE_UNVERIFIABLE to test this path")
def test_get_arv_unverifiable_address_asks_for_facts():
    with pytest.raises(service.ArvError) as exc:
        service.get_arv(UNVERIFIABLE_ADDRESS)
    assert exc.value.code in ("ADDRESS_NOT_VERIFIED", "ADDRESS_NOT_FOUND")


def test_get_arv_fake_address_not_found():
    with pytest.raises(service.ArvError) as exc:
        service.get_arv(FAKE_ADDRESS)
    assert exc.value.code == "ADDRESS_NOT_FOUND"
