"""Tests for server.py — MCP tool registration, schemas, the prompt, and how
tool calls map service results and errors (service mocked)."""
import anyio
import pytest

from arv_mcp import server, service


def _run(coro_fn, *a, **kw):
    return anyio.run(lambda: coro_fn(*a, **kw))


def test_tools_and_schemas():
    tools = {t.name: t for t in _run(server.server.list_tools)}
    assert set(tools) == {"get_arv", "lookup_property", "get_comp_map"}
    get_arv = tools["get_arv"]
    assert get_arv.input_schema["required"] == ["address"]
    assert {"sqft", "property_type", "latitude", "longitude", "radius_mi", "days_back",
            "include_market_context", "max_comps", "has_pool"} <= set(get_arv.input_schema["properties"])
    assert "renovated_range" in get_arv.description and "ADDRESS_NOT_VERIFIED" in get_arv.description
    for t in tools.values():
        assert t.annotations.read_only_hint is True


def test_prompt_registered():
    prompts = _run(server.server.list_prompts)
    assert [p.name for p in prompts] == ["analyze_arv"]
    text = server.analyze_arv("1 Main St, Mesa, AZ")
    assert "1 Main St, Mesa, AZ" in text and "get_arv" in text


def test_instructions_set():
    assert "not an appraisal" in server.INSTRUCTIONS


def test_get_arv_passes_arguments(monkeypatch):
    seen = {}

    def fake(address, **kw):
        seen.update(address=address, **kw)
        return {"estimate": {"arv": 1}}

    monkeypatch.setattr(service, "get_arv", fake)
    out = _run(server.get_arv, "1 Main St", sqft=1500, has_pool=True, radius_mi=2.0)
    assert out == {"estimate": {"arv": 1}}
    assert seen["address"] == "1 Main St" and seen["sqft"] == 1500 and seen["has_pool"] is True
    assert seen["radius_mi"] == 2.0 and seen["include_market_context"] is True


def test_service_errors_become_structured_results(monkeypatch):
    def fail(address, **kw):
        raise service.ArvError("ADDRESS_NOT_VERIFIED", "ask for facts", needs=["sqft"])

    monkeypatch.setattr(service, "get_arv", fail)
    out = _run(server.get_arv, "1 Main St")
    assert out == {"error": {"code": "ADDRESS_NOT_VERIFIED", "message": "ask for facts", "needs": ["sqft"]}}


def test_unexpected_errors_never_leak_tracebacks(monkeypatch):
    def crash(address):
        raise KeyError("boom")

    monkeypatch.setattr(service, "lookup_property", crash)
    out = _run(server.lookup_property, "1 Main St")
    assert out["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in out["error"]["message"]


@pytest.mark.parametrize("name", ["get_arv", "lookup_property"])
def test_call_tool_end_to_end(monkeypatch, name):
    monkeypatch.setattr(service, name, lambda address, **kw: {"ok": address})
    result = _run(server.server.call_tool, name, {"address": "1 Main St"})
    assert "1 Main St" in str(result)


# ── Comps map (MCP Apps) ──────────────────────────────────────────────────────

def test_get_arv_opens_the_comps_map_and_map_tool_is_app_only():
    tools = {t.name: t for t in _run(server.server.list_tools)}
    assert tools["get_arv"].meta == {"ui": {"resourceUri": server.COMP_MAP_URI}}
    assert tools["get_comp_map"].meta["ui"] == {"resourceUri": server.COMP_MAP_URI, "visibility": ["app"]}
    assert tools["get_comp_map"].annotations.read_only_hint is True
    assert "lookup_property" in tools and not (tools["lookup_property"].meta or {}).get("ui")


def test_comp_map_resource_is_served_with_its_csp():
    (res,) = _run(server.server.list_resources)
    assert res.uri == server.COMP_MAP_URI
    assert res.mime_type == "text/html;profile=mcp-app"
    domains = res.meta["ui"]["csp"]["resourceDomains"]
    assert set(domains) == {"https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com",
                            "https://tile.openstreetmap.org", "https://server.arcgisonline.com"}
    assert "connectDomains" not in res.meta["ui"]["csp"]  # the page never fetches; tiles are images
    (content,) = _run(server.server.read_resource, server.COMP_MAP_URI)
    html = content.content
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "ui/initialize" in html and "get_comp_map" in html


def test_comp_map_page_only_loads_from_declared_domains_with_integrity():
    import re
    (content,) = _run(server.server.read_resource, server.COMP_MAP_URI)
    html = content.content
    declared = set(server.COMP_MAP_CSP.resource_domains)
    for tag in re.findall(r"<(?:script|link)[^>]*>", html):
        url = re.search(r'(?:src|href)="([^"]+)"', tag)
        if not url or not url.group(1).startswith("http"):
            continue
        origin = "/".join(url.group(1).split("/")[:3])
        assert origin in declared, f"{origin} is not in the page's CSP"
        assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag
    for origin in re.findall(r"https://[a-z0-9.-]+", html):
        if origin in ("https://www.openstreetmap.org",):  # attribution link, not a load
            continue
        assert origin in declared, f"{origin} is referenced but not declared"


def test_get_comp_map_tool_passes_arguments(monkeypatch):
    seen = {}

    def fake(address, **kw):
        seen.update(address=address, **kw)
        return {"subject": {}}

    monkeypatch.setattr(service, "get_comp_map", fake)
    assert _run(server.get_comp_map, "1 Main St", radius_mi=2.0, has_pool=True) == {"subject": {}}
    assert seen["address"] == "1 Main St" and seen["radius_mi"] == 2.0 and seen["has_pool"] is True
