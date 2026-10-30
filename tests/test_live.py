"""Live smoke tests against Realtor.com (via HomeHarvest) and the Census
geocoder. Deselected by default; run with:

    pytest -m live

They guard the HomeHarvest internals data/client.py depends on (single-home
lookup, coordinate-centered search) and the end-to-end get_arv paths. Data
changes daily, so assertions are about shape and behaviour, not values.
"""
import pytest
from homeharvest.core.scrapers.models import ListingType

from arv_mcp import service
from arv_mcp.data import client
from arv_mcp.data.geocode import census_geocode

pytestmark = pytest.mark.live

SOLD_ADDRESS = "1234 Main St, Mesa, AZ 85204"


def test_geocoder_lookup_and_home_fetch():
    loc = client.suggest_location(SOLD_ADDRESS)
    assert loc["area_type"] == "address" and loc["mpr_id"]
    home = client.fetch_home(loc["mpr_id"])
    assert home.address.street == "1234 Main St"


def test_coordinate_search_respects_radius_and_days():
    homes = client.search_near(33.4, -111.8, listing_type=ListingType.SOLD, radius_mi=1.0,
                               past_days=90, sort_by="sold_date", limit=50)
    assert homes
    assert all(h.latitude is not None for h in homes)


def test_census_geocoder():
    hit = census_geocode(SOLD_ADDRESS)
    assert hit and abs(hit["latitude"] - 33.39) < 0.01
    assert census_geocode("99999 E Main St, Mesa, AZ 85204") is None


def test_get_arv_verified_subject():
    r = service.get_arv(SOLD_ADDRESS)
    assert r["subject"]["subject_source"] == "record"
    assert r["estimate"]["arv"] > 0
    assert r["eligible_comps"]
    assert all("1234 Main" not in c["address"] for c in r["eligible_comps"])


def test_get_arv_unverifiable_address_asks_for_facts():
    with pytest.raises(service.ArvError) as exc:
        service.get_arv("500 Park Ln Unit 1433, Mesa, AZ 85209")
    assert exc.value.code in ("ADDRESS_NOT_VERIFIED", "ADDRESS_NOT_FOUND")


def test_get_arv_fake_address_not_found():
    with pytest.raises(service.ArvError) as exc:
        service.get_arv("99999 E Main St, Mesa, AZ 85204")
    assert exc.value.code == "ADDRESS_NOT_FOUND"
