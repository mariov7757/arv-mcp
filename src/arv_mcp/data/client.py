"""
client.py — the only code that talks to Realtor.com, via HomeHarvest.

Wraps HomeHarvest's RealtorScraper instead of calling scrape_property(), for
three reasons found by reading its internals (homeharvest 0.8.x):

  1. Its GraphQL POST has no timeout and raises on 403 at once. _graphql_post
     is overridden here to add a timeout, draw a rate-limit token for every
     real request (including HomeHarvest's internal paging), and retry a
     403/429 once with backoff before failing as UpstreamError.
  2. Its single-home lookup (handle_home) swallows every exception and
     returns [], which would turn a block or rate limit into "address not
     found". Errors are recorded on the scraper and re-raised after the call.
  3. Its radius search is coordinate-based internally (coordinates + radius),
     but only takes coordinates from its own text geocoder. Overriding
     handle_location() lets any lat/lon drive the search, so comp searches
     never re-geocode the address.

Pin homeharvest to a tested range (pyproject.toml): items 2 and 3 rely on
RealtorScraper internals, and the `-m live` smoke test guards them.
"""

import json
import logging
import time

import requests
from homeharvest.core.scrapers import DEFAULT_HEADERS, ScraperInput
from homeharvest.core.scrapers.models import ListingType, ReturnType, SearchPropertyType
from homeharvest.core.scrapers.realtor import RealtorScraper
from tenacity import RetryError

from ..ratelimit import max_wait_s, realtor_limiter

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 20
RETRY_STATUSES = {403, 429}
MAX_RETRY_WAIT_S = 10.0


class UpstreamError(Exception):
    """Realtor.com refused, rate-limited or failed a request (after one retry)."""


class _Scraper(RealtorScraper):
    """RealtorScraper with a timeout, rate limiting, one retry, error
    recording, and an optional fixed search centroid."""

    def __init__(self, scraper_input: ScraperInput, centroid: tuple[float, float] | None = None):
        super().__init__(scraper_input)
        self._centroid = centroid
        self.errors: list[Exception] = []

    def handle_location(self):
        if self._centroid is None:
            return super().handle_location()
        lat, lon = self._centroid
        return {"text": f"{lat},{lon}", "area_type": "address",
                "centroid": {"lat": lat, "lon": lon}, "mpr_id": None}

    def _graphql_post(self, query: str, variables: dict, operation_name: str) -> dict:
        payload = json.dumps(
            {"operationName": operation_name, "query": self._minify_query(query), "variables": variables},
            separators=(",", ":"),
        )
        try:
            for attempt in (1, 2):
                realtor_limiter.acquire(max_wait_s())
                try:
                    resp = requests.post(self.SEARCH_GQL_URL, headers=DEFAULT_HEADERS, data=payload,
                                         proxies=self.proxies, timeout=REQUEST_TIMEOUT_S)
                except requests.RequestException as e:
                    if attempt == 2:
                        raise UpstreamError(f"Realtor.com request failed: {e}") from e
                    time.sleep(1.0)
                    continue
                if resp.status_code in RETRY_STATUSES:
                    if attempt == 2:
                        raise UpstreamError(
                            f"Realtor.com returned HTTP {resp.status_code} "
                            f"({'blocked' if resp.status_code == 403 else 'rate limited'}).")
                    time.sleep(_retry_after(resp))
                    continue
                if resp.status_code >= 400:
                    raise UpstreamError(f"Realtor.com returned HTTP {resp.status_code}.")
                try:
                    return resp.json()
                except ValueError as e:
                    raise UpstreamError("Realtor.com returned a non-JSON response.") from e
            raise UpstreamError("Realtor.com request failed.")  # pragma: no cover - loop always returns/raises
        except Exception as e:
            self.errors.append(e)
            raise


def _retry_after(resp) -> float:
    try:
        return min(MAX_RETRY_WAIT_S, max(1.0, float(resp.headers.get("Retry-After", 2))))
    except (TypeError, ValueError):
        return 2.0


def _unwrap(e: Exception) -> Exception:
    """tenacity wraps exceptions from HomeHarvest's retried methods in RetryError."""
    if isinstance(e, RetryError) and e.last_attempt is not None and e.last_attempt.exception():
        return e.last_attempt.exception()
    return e


def _input(**kwargs) -> ScraperInput:
    kwargs.setdefault("return_type", ReturnType.pydantic)
    # The base search already returns tags and MLS details (verified live:
    # identical tag/detail coverage either way); the "extra" bulk-details
    # request only adds a call that HomeHarvest wraps in its own retries.
    kwargs.setdefault("extra_property_data", False)
    return ScraperInput(**kwargs)


def suggest_location(address: str) -> dict | None:
    """Realtor.com's own geocoder for free text: {"text", "area_type",
    "centroid": {"lat", "lon"}, "mpr_id", ...} or None. area_type "address"
    means it resolved a specific property (mpr_id), which is not necessarily
    the one asked for — callers must verify."""
    scraper = _Scraper(_input(location=address, listing_type=None))
    # HomeHarvest wraps handle_location in a 3-attempt tenacity retry with
    # backoff; call the undecorated function so a rate limit fails fast (our
    # _graphql_post already retries 403/429 once).
    handle = getattr(RealtorScraper.handle_location, "__wrapped__", None)
    try:
        return handle(scraper) if handle else scraper.handle_location()
    except Exception as e:
        raise _unwrap(e) from None


def fetch_home(property_id: str):
    """One home's full record by Realtor.com property id, whatever its status
    (for sale, pending, sold, off market). None if not found."""
    scraper = _Scraper(_input(location=property_id, listing_type=None))
    homes = scraper.handle_home(property_id)
    if scraper.errors:
        raise _unwrap(scraper.errors[0])
    return homes[0] if homes else None


def search_near(lat: float, lon: float, *, listing_type: ListingType | None, radius_mi: float,
                property_types: list[str] | None = None, sqft_min: int | None = None,
                sqft_max: int | None = None, past_days: int | None = None, sort_by: str | None = None,
                limit: int = 500, exclude_pending: bool = False) -> list:
    """Radius search centered on (lat, lon). listing_type None means every
    common status. Returns pydantic Property objects."""
    si = _input(
        location=f"{lat},{lon}",
        listing_type=listing_type,
        radius=radius_mi,
        property_type=[SearchPropertyType[t.upper()] for t in property_types] if property_types else None,
        sqft_min=sqft_min,
        sqft_max=sqft_max,
        last_x_days=past_days,
        sort_by=sort_by,
        sort_direction="desc",
        limit=limit,
        exclude_pending=exclude_pending,
    )
    scraper = _Scraper(si, centroid=(lat, lon))
    try:
        homes = scraper.search()
    except Exception as e:
        raise _unwrap(e) from None
    if scraper.errors:
        raise _unwrap(scraper.errors[0])
    return homes or []
