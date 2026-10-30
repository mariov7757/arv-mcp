# arv-mcp

<!-- mcp-name: io.github.mariov7757/arv-mcp -->

An [MCP](https://modelcontextprotocol.io) server that estimates a home's
**after-repair value (ARV)** from nearby sold comps, and hands the model the
scored evidence behind the number so it can explain, check and challenge it.

Ask Claude *"What's the ARV of 1234 Main St?"* and it gets back a typical
value, a renovated range, a confidence tier, the comps that support it (and
the ones that were thrown out, with reasons), what's listed nearby right
now, and warnings about anything that should make it cautious.

```
You:    What's the ARV of 1234 Main Street?

Claude: Typical value is about $516k ($263/sqft on 1,961 sqft), based on
        20 eligible sales, 5+ of them within a quarter mile (confidence 5/5).
        Renovated comps support $541k–$586k. The closest match, 1260 Main
        Street, sold 23 days ago for $465k ($272/sqft): same subdivision,
        a bit smaller, no pool.

        Worth knowing: this house itself sold this week for $432,500
        ($220/sqft), well under the comp-based value. That usually means it
        sold in rough condition, which fits a flip at the renovated range.
        Two of the eligible comps were distressed sales; without them the
        median barely moves.
```

> **Not an appraisal.** Data comes from public listings on Realtor.com via
> [HomeHarvest](https://github.com/ZacharyHampton/HomeHarvest) and can be
> wrong or missing. Use it for research and as a starting point, not for
> lending or legal decisions.

## How it works

```
address ──► verify the property ──► sold comps ──► score & gate ──► estimate
               │  (any status)        within 1.5 mi    each comp       + evidence
               └─ not verified? ──► locate it + ask for its facts
```

1. **Find the subject.** The address is resolved to Realtor.com's record
   for exactly that street and unit, whether it's for sale, pending, sold
   or off market. Realtor.com's geocoder will happily return the wrong
   house for a bad address, so every result is checked against the street
   and unit typed. If nothing verifies, the address is located (Census
   geocoder, or caller-supplied lat/lon) and the tool asks for the facts it
   needs (`sqft`, `property_type`) instead of guessing.
2. **Pull comps around the subject's coordinates:** sold in the last 180
   days within 1.5 miles (both adjustable per call), ±40% of the subject's
   size, same property type. The subject's own sale is pulled out and
   reported separately, so a just-sold house can't comp itself.
3. **Score every comp** on 13 factors: size, year built, lot, beds/baths,
   property type, recency, condition signals, distance, subdivision,
   garage, stories, pool and basement. Each known factor also has a
   **floor**. A comp that fails any one (a different property type, a much
   bigger house, one built decades apart) is excluded outright, however good
   it looks elsewhere. Size, age and lot floors loosen for comps right next
   door and tighten with distance.
4. **Check the micro-market.** Each comp's subdivision median (or its own
   $/sqft) must sit within ±20% of the subject's local price level, so a
   great physical match in a pricier pocket doesn't inflate the estimate.
   Subdivisions come from the MLS "Subdivision" field, with phases like
   "Unit 2" or "Ph 04 Sec 01" merged.
5. **Estimate:**
   - **Typical value:** similarity-weighted median $/sqft × subject sqft.
   - **Renovated range:** weighted 75th–90th percentile, what the top of
     the pool supports.
   - **Confidence 1–5:** how close the supporting comps are.

   If no comp survives, the tool falls back to the area's going rate and
   says plainly that the number isn't comp-backed.

## Tools

### `get_arv`

| Parameter | Description |
|---|---|
| `address` (required) | Full street address, including any unit number. |
| `sqft`, `property_type` | Override the record. Required when the address can't be verified. |
| `beds`, `baths`, `year_built`, `lot_sqft`, `stories`, `garage`, `has_pool` | Override or supply facts; also how what-ifs work ("what if it had a pool?"). |
| `latitude`, `longitude` | Locate a property no geocoder can find. |
| `radius_mi`, `days_back` | Comp search window: 0.25–5 mi (default 1.5), 30–730 days (default 180). |
| `include_market_context` | Also fetch nearby active/pending listings (default true). |
| `max_comps` | Eligible comps to return, 1–25 (default 10). |

Returns `subject`, `estimate` (`arv`, `renovated_range`, `arv_fallback`,
window), `confidence`, `pool_stats`, `eligible_comps` (each with the factors
that cost it points and any distress flags), `excluded_comps` (with
reasons), `market_context` and `guidance`. Unknown facts are `null`, never
`0`.

#### Comps map

In hosts that support [MCP Apps](https://modelcontextprotocol.io/extensions/apps)
(for example Claude on the web and desktop), `get_arv` also opens an
interactive map next to the answer:

- the subject as an orange home pin
- sold comps as price tags: green ⭐ for comps that fed the estimate, blue for
  the rest
- active and pending listings as teal 🏠 and purple ⏳ tags, with ⭐ when they
  would qualify as comps
- a popup on every tag with its facts and, for comps that weren't used, the
  reason
- Street/Satellite basemaps, a "Sold within" filter (3M, 6M, 1Y, All) and a
  layer toggle for active/pending listings

The map gets its data through an app-only tool, `get_comp_map`, which the
model never sees. Opening it reuses `get_arv`'s cached data, so it costs no
extra requests. Clients without MCP Apps support just get the normal
`get_arv` result.

### `lookup_property`

The verified record for an address: size, beds/baths, year, lot, stories,
garage, pool, subdivision, status, list price (only while it's on the
market) and last sale.

### Prompt: `analyze_arv`

An appraiser-style walkthrough (subject → comps → adjustments → range).
Shows up as a slash command in Claude Code.

### Errors

Failures come back as `{"error": {"code", "message", ...}}` so the model
can act on them:

| Code | Meaning |
|---|---|
| `ADDRESS_NOT_VERIFIED` | Located, but no record matches; `needs` lists the facts to ask for. |
| `ADDRESS_NOT_FOUND` | Couldn't find or locate the address. |
| `MISSING_FACTS` | The record lacks something the estimate needs (e.g. sqft). |
| `UNSUPPORTED_PROPERTY_TYPE` | Vacant land: no living area to price per sqft. |
| `NO_COMPS` | No sales in the window; try a larger `radius_mi` / `days_back`. |
| `RATE_LIMITED` | Local rate limit; `retry_after_s` says when to retry. |
| `UPSTREAM_ERROR` | Realtor.com refused or failed the request. |

## Install

**Claude Desktop: one-click install (recommended)**

1. Download [`arv-mcp.mcpb`](https://github.com/mariov7757/arv-mcp/releases/latest/download/arv-mcp.mcpb)
   from the latest release.
2. Double-click it, or drag it into Claude Desktop's **Settings → Extensions**,
   and click **Install**.

That's it: no terminal, no config file, and nothing else to install. Claude
Desktop sets up Python and the dependencies itself; the first launch takes
about half a minute while it does.

**For developers**

Requires Python 3.11+. Published on [PyPI](https://pypi.org/project/arv-mcp/)
and listed in the [MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.mariov7757/arv-mcp`.

**Claude Code**

```bash
claude mcp add arv-mcp -- uvx arv-mcp
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "arv-mcp": {
      "command": "uvx",
      "args": ["arv-mcp"]
    }
  }
}
```

**pip**

```bash
pip install arv-mcp
```

To run the unreleased `main` branch instead, use
`uvx --from git+https://github.com/mariov7757/arv-mcp arv-mcp`.

**From a clone**

```bash
git clone https://github.com/mariov7757/arv-mcp && cd arv-mcp
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
claude mcp add arv-mcp -- "$PWD/.venv/bin/arv-mcp"
```

**Windows: if `uvx` fails installing `pywin32`** (`os error 32` /
"being used by another process", usually antivirus scanning uv's temporary
build folder), install into a regular virtualenv instead and point the
client at the installed executable:

```powershell
uv venv $HOME\.arv-mcp
uv pip install --python $HOME\.arv-mcp\Scripts\python.exe arv-mcp
claude mcp add arv-mcp -- $HOME\.arv-mcp\Scripts\arv-mcp.exe
```

## Configuration

All optional, via environment variables:

| Variable | Default | |
|---|---|---|
| `ARV_MCP_REQUESTS_PER_MIN` | `30` | Realtor.com requests per minute (token bucket)… |
| `ARV_MCP_REQUEST_BURST` | `10` | …and how many can go at once. |
| `ARV_MCP_GEOCODER_PER_SEC` | `1` | Census geocoder requests per second. |
| `ARV_MCP_RATE_WAIT_S` | `15` | Longest a call waits for a rate-limit slot before returning `RATE_LIMITED`. |
| `ARV_MCP_MAX_CONCURRENT` | `2` | Tool calls processed at once. |
| `ARV_MCP_LOG_LEVEL` | `WARNING` | Logs go to stderr. |

A full `get_arv` makes 5 requests to Realtor.com (2 to look up the subject,
1 for sold comps, 2 for active/pending) and takes ~4–6 s. Results are cached
for 15 minutes, so follow-ups and what-ifs on the same address return
instantly.

## Limitations

- **Tuned on Phoenix metro.** Gate bands and confidence tiers were
  calibrated on ~8,000 Phoenix-area valuations. The engine runs anywhere in
  the US, but in other markets (especially rural or low-volume ones) lean
  on the comps and confidence tier, and widen `radius_mi` / `days_back`.
- **Typical value isn't condition-aware.** Distressed sales are flagged
  (MLS REO/short-sale conditions, "fixer"/"as-is" language) and their
  effect on the median is reported, but they aren't excluded.
- **$/sqft is the wrong tool for some properties.** Land is refused; farms,
  multi-family and mobile homes get a warning.
- **Unofficial data source.** HomeHarvest scrapes Realtor.com's public
  site. Requests run from your own machine and are rate-limited, but the
  site can change or block access at any time. `homeharvest` is pinned to
  a tested range because the client relies on its internals.

## Development

```bash
pip install -e ".[dev]"
pytest            # offline suite; real network calls are blocked
ARV_MCP_LIVE_ADDRESS="<a real address>" pytest -m live   # smoke tests against Realtor.com + Census
ruff check src tests
```

```
src/arv_mcp/
  server.py         MCP tools, instructions and prompt
  service.py        address in, estimate + evidence out
  arv.py            eligibility, weighted median, renovated range, confidence
  scoring.py        per-comp gated scoring
  comps.py          comp normalization and bad-data filters
  ratelimit.py      token buckets
  data/client.py    Realtor.com transport over HomeHarvest
  data/homeharvest.py  subject lookup, comp searches, field extraction
  data/geocode.py   Census geocoder
```

### Releasing

The version lives in one place: `__version__` in `src/arv_mcp/__init__.py`.

1. Bump `__version__` (patch for fixes, minor for new tools, parameters or
   fields) and merge to `main` with CI green.
2. Tag the release and push the tag:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

The [Release workflow](.github/workflows/release.yml) then:

- checks that the tag matches `__version__`
- runs lint and tests, and builds the package
- publishes to PyPI with Trusted Publishing (no API token)
- stamps the version into `server.json` and publishes it to the
  [MCP Registry](https://registry.modelcontextprotocol.io) as
  `io.github.mariov7757/arv-mcp`, authenticated by GitHub OIDC
- builds the Claude Desktop extension (`arv-mcp.mcpb`, see
  [`scripts/build_mcpb.py`](scripts/build_mcpb.py)), validates it with the
  official `mcpb` CLI, and attaches it to a GitHub Release

To build the extension locally (needs [uv](https://docs.astral.sh/uv/)):
`python scripts/build_mcpb.py`, which writes `dist/arv-mcp.mcpb`.

PyPI versions are permanent. A broken release gets yanked on PyPI and
replaced by the next patch version; a version number is never reused.

## License

MIT
