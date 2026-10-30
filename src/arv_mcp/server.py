"""
server.py — the MCP server: tools, instructions, a prompt and a comps-map
view over service.py.

Run with the `arv-mcp` command (stdio transport). Tool calls run the
blocking HomeHarvest work in worker threads, at most ARV_MCP_MAX_CONCURRENT
at a time, so a slow scrape never freezes the server. In hosts that support
MCP Apps, get_arv's result also opens ui/comp_map.html, which fetches its
data through the app-only get_comp_map tool.
"""

import logging
import os
import sys
from importlib import resources
from typing import Annotated, Any

import anyio
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, service

log = logging.getLogger("arv_mcp")

INSTRUCTIONS = """\
arv-mcp estimates a US residential property's after-repair value (ARV) from
nearby sold comps (Realtor.com data via HomeHarvest) and returns the scored
evidence behind the number.

- Use get_arv for value questions ("what's the ARV / what's it worth / what
  would it sell for"). Use lookup_property only to check a property's facts.
- The estimate is a starting point, not an appraisal. Always explain it from
  the comps and say how confident it is.
- Scoring was tuned on Phoenix-metro data; in other markets lean harder on
  the comps themselves and the confidence tier.
- Data is scraped from public listings and can be wrong or missing; never
  present it as authoritative.
"""

GET_ARV_DESCRIPTION = """\
Estimate a property's value from nearby sold comps and return the evidence.

How to use the result:
- estimate.arv is TYPICAL value (similarity-weighted median of eligible sold
  comps' $/sqft x subject sqft). estimate.renovated_range is what the top of
  the eligible pool supports, i.e. the ARV a flip finished to the top of the
  neighbourhood could target. Quote both, labelled. If renovated_range is
  null, say there weren't enough comps to separate renovated sales.
- Treat the estimate as one input: check it against the closest, highest-
  scoring eligible_comps and say whether they agree. Use each comp's
  `factors` to explain differences (size, age, pool...).
- Read `guidance` first and pass its warnings on. In particular:
  arv_fallback=true means NOT comp-backed; low confidence.tier (1-2) means
  thin evidence; distressed comps (REO/short sale) pull prices down; a
  subject_source of "user_supplied" means the facts weren't verified.
- market_context is current asking prices, a ceiling check only, never comps.
- If subject.address differs from what the user asked (address_fuzzy_match),
  confirm it's the right property.
- For what-ifs ("what if it had a pool / were 1,800 sqft"), call again with
  that fact overridden; cached data makes repeat calls fast.

Errors come back as {"error": {"code", "message", ...}}:
- ADDRESS_NOT_VERIFIED: the address was located but no record matches it.
  Ask the user for the facts in `needs` (and ideally `recommended`), then
  call again with them.
- ADDRESS_NOT_FOUND: ask the user to check the address, or pass latitude and
  longitude with the facts.
- MISSING_FACTS: ask for the listed facts and call again.
- UNSUPPORTED_PROPERTY_TYPE (vacant land), NO_COMPS (try a larger radius_mi
  or days_back), RATE_LIMITED (wait retry_after_s), UPSTREAM_ERROR (data
  source unavailable): explain to the user.
"""

LOOKUP_DESCRIPTION = """\
Look up the verified Realtor.com record for an address: size, beds/baths,
year built, lot, stories, garage, pool, subdivision, status, list price (when
on the market) and last sale. Works for sold, off-market and for-sale homes.
Returns ADDRESS_NOT_VERIFIED / ADDRESS_NOT_FOUND errors when no record matches
the exact street and unit. Use get_arv for valuations.
"""

COMP_MAP_DESCRIPTION = """\
Comps-map data for the get_arv map view: the subject and every sold comp and
active/pending listing with coordinates and marker/popup HTML. Called by the
map itself, not by the model.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

_LOG_LEVEL = os.environ.get("ARV_MCP_LOG_LEVEL", "WARNING").upper()
if _LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    _LOG_LEVEL = "WARNING"

_capacity = anyio.CapacityLimiter(max(1, int(os.environ.get("ARV_MCP_MAX_CONCURRENT", "2"))))

# ── Comps map (MCP Apps) ──────────────────────────────────────────────────────
# Hosts that support MCP Apps render ui/comp_map.html next to get_arv's result;
# other clients just get get_arv's normal result. The page loads Leaflet and
# its marker plugin from pinned, integrity-checked CDN files and draws map
# tiles from OpenStreetMap (street) and Esri (satellite); nothing else may load.
COMP_MAP_URI = "ui://arv-mcp/comp-map.html"
COMP_MAP_CSP = ResourceCsp(resource_domains=[
    "https://cdn.jsdelivr.net",       # Leaflet 1.9.3, Font Awesome 6.2.0
    "https://cdnjs.cloudflare.com",   # Leaflet.awesome-markers 2.0.2
    "https://tile.openstreetmap.org",
    "https://server.arcgisonline.com",
])
apps = Apps()
apps.add_html_resource(
    COMP_MAP_URI,
    (resources.files("arv_mcp") / "ui" / "comp_map.html").read_text(encoding="utf-8"),
    name="comp_map",
    title="Comps map",
    description="Map of the subject, its sold comps and nearby active/pending listings.",
    csp=COMP_MAP_CSP,
)


async def _run(fn, *args, **kwargs) -> dict[str, Any]:
    def call():
        try:
            return fn(*args, **kwargs)
        except service.ArvError as e:
            return {"error": e.to_dict()}
        except Exception:  # noqa: BLE001 - never leak a stack trace to the model
            log.exception("Unexpected error in %s", getattr(fn, "__name__", fn))
            return {"error": {"code": "INTERNAL_ERROR",
                              "message": "Unexpected error while estimating; see the server log."}}

    return await anyio.to_thread.run_sync(call, limiter=_capacity)


@apps.tool(resource_uri=COMP_MAP_URI, name="get_arv", title="Estimate ARV from sold comps",
           description=GET_ARV_DESCRIPTION, annotations=READ_ONLY)
async def get_arv(
    address: Annotated[str, Field(description="Full street address, e.g. '1234 E Main St, Mesa, AZ 85204'. "
                                              "Include the unit number for condos/townhomes.")],
    sqft: Annotated[int | None, Field(description="Living area sqft. Overrides the record; required when "
                                                  "the address can't be verified.")] = None,
    property_type: Annotated[str | None, Field(description="single_family, condo, townhome, multi_family, "
                                                           "mobile, farm or land. Required when the address "
                                                           "can't be verified.")] = None,
    beds: Annotated[float | None, Field(description="Bedrooms (override).")] = None,
    baths: Annotated[float | None, Field(description="Bathrooms, halves as .5 (override).")] = None,
    year_built: Annotated[int | None, Field(description="Year built (override).")] = None,
    lot_sqft: Annotated[int | None, Field(description="Lot size in sqft (override).")] = None,
    stories: Annotated[int | None, Field(description="Levels in the unit (override).")] = None,
    garage: Annotated[float | None, Field(description="Garage spaces (override).")] = None,
    has_pool: Annotated[bool | None, Field(description="Private pool (override).")] = None,
    latitude: Annotated[float | None, Field(description="Only when the address can't be located; "
                                                        "pass with longitude.")] = None,
    longitude: Annotated[float | None, Field(description="Only when the address can't be located; "
                                                         "pass with latitude.")] = None,
    radius_mi: Annotated[float | None, Field(description="Comp search radius in miles (0.25-5, default 1.5). "
                                                         "Widen for rural or thin markets.")] = None,
    days_back: Annotated[int | None, Field(description="How far back to look for sales, in days (30-730, "
                                                       "default 180).")] = None,
    include_market_context: Annotated[bool, Field(description="Also fetch nearby active/pending listings "
                                                              "(two extra requests).")] = True,
    max_comps: Annotated[int | None, Field(description="How many eligible comps to return (1-25, "
                                                       "default 10).")] = None,
) -> dict[str, Any]:
    return await _run(service.get_arv, address, sqft=sqft, property_type=property_type, beds=beds, baths=baths,
                      year_built=year_built, lot_sqft=lot_sqft, stories=stories, garage=garage, has_pool=has_pool,
                      latitude=latitude, longitude=longitude, radius_mi=radius_mi, days_back=days_back,
                      include_market_context=include_market_context, max_comps=max_comps)


@apps.tool(resource_uri=COMP_MAP_URI, visibility=["app"], name="get_comp_map", title="Comps map data",
           description=COMP_MAP_DESCRIPTION, annotations=READ_ONLY)
async def get_comp_map(
    address: str,
    sqft: int | None = None,
    property_type: str | None = None,
    beds: float | None = None,
    baths: float | None = None,
    year_built: int | None = None,
    lot_sqft: int | None = None,
    stories: int | None = None,
    garage: float | None = None,
    has_pool: bool | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_mi: float | None = None,
    days_back: int | None = None,
) -> dict[str, Any]:
    return await _run(service.get_comp_map, address, sqft=sqft, property_type=property_type, beds=beds,
                      baths=baths, year_built=year_built, lot_sqft=lot_sqft, stories=stories, garage=garage,
                      has_pool=has_pool, latitude=latitude, longitude=longitude, radius_mi=radius_mi,
                      days_back=days_back)


# Extensions are fixed when the server is constructed, so this comes after the
# app-bound tools and resource above are registered on `apps`.
server = MCPServer("arv-mcp", instructions=INSTRUCTIONS, version=__version__, log_level=_LOG_LEVEL,
                   extensions=[apps])


@server.tool(name="lookup_property", title="Look up a property's facts", description=LOOKUP_DESCRIPTION,
             annotations=READ_ONLY)
async def lookup_property(
    address: Annotated[str, Field(description="Full street address, including any unit number.")],
) -> dict[str, Any]:
    return await _run(service.lookup_property, address)


@server.prompt(name="analyze_arv", title="Appraiser-style ARV walkthrough",
               description="Walk through a property's ARV like an appraiser: subject, comps, adjustments, range.")
def analyze_arv(address: str) -> str:
    return (
        f"Estimate the after-repair value of {address}. Call get_arv for it, then walk through it like an "
        "appraiser:\n"
        "1. Subject: confirm the matched address and its key facts; flag anything overridden, unverified or "
        "missing.\n"
        "2. Comps: take the 3-5 closest, highest-scoring eligible comps. For each give price, $/sqft, distance, "
        "how recently it sold and how it differs from the subject (use its factors).\n"
        "3. Adjustments: say which differences push value up or down and roughly how much.\n"
        "4. Conclusion: typical value and renovated range, the confidence tier, and every warning in guidance. "
        "Use market_context only as a ceiling check.\n"
        "If get_arv returns an error, follow its instructions (for example, ask me for the property's details)."
    )


def main() -> None:
    logging.basicConfig(level=_LOG_LEVEL, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server.run("stdio")


if __name__ == "__main__":
    main()
