"""Tests for the Claude Desktop extension bundle (scripts/build_mcpb.py and
packaging/mcpb/): the manifest validates against the vendored MCPB v0.4
schema, matches the package, and the built .mcpb has the right contents."""
import importlib.util
import json
import tomllib
import zipfile
from pathlib import Path

import anyio
import jsonschema
import pytest

import arv_mcp
from arv_mcp import server

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "packaging" / "mcpb" / "manifest-v0.4.schema.json").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "scripts" / "build_mcpb.py")
build_mcpb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_mcpb)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> zipfile.ZipFile:
    out = build_mcpb.build(tmp_path_factory.mktemp("dist"), lock=False)
    return zipfile.ZipFile(out)


def test_manifest_is_valid_and_versioned_from_the_package():
    manifest = build_mcpb.stamped_manifest()
    jsonschema.validate(manifest, SCHEMA)
    assert manifest["version"] == arv_mcp.__version__
    assert manifest["name"] == PYPROJECT["project"]["name"]
    assert manifest["license"] == PYPROJECT["project"]["license"]


def test_server_runs_the_packaged_command_with_uv():
    server_cfg = build_mcpb.stamped_manifest()["server"]
    assert server_cfg["type"] == "uv"
    assert (ROOT / server_cfg["entry_point"]).is_file()
    cfg = server_cfg["mcp_config"]
    assert cfg["command"] == "uv"
    assert cfg["args"][:3] == ["run", "--directory", "${__dirname}"]
    assert "--frozen" in cfg["args"]
    assert cfg["args"][-1] in PYPROJECT["project"]["scripts"]


def test_manifest_lists_the_tools_the_model_sees():
    tools = anyio.run(server.server.list_tools)
    model_tools = {t.name for t in tools if "model" in ((t.meta or {}).get("ui", {}).get("visibility") or ["model"])}
    assert {t["name"] for t in build_mcpb.stamped_manifest()["tools"]} == model_tools


def test_privacy_policies_cover_the_outside_services():
    policies = build_mcpb.stamped_manifest()["privacy_policies"]
    assert any("realtor.com" in p for p in policies)
    assert any("census.gov" in p for p in policies)


def test_bundle_contents(bundle):
    names = set(bundle.namelist())
    assert {"manifest.json", "pyproject.toml", "README.md", "LICENSE", "icon.png",
            "src/arv_mcp/__init__.py", "src/arv_mcp/server.py", "src/arv_mcp/ui/comp_map.html"} <= names
    assert not any("__pycache__" in n or n.endswith(".pyc") or n.startswith((".venv/", "server/lib/"))
                   for n in names)
    assert not any(n.startswith(("tests/", "scripts/", "packaging/", ".github/")) for n in names)
    assert bundle.read("icon.png").startswith(b"\x89PNG\r\n\x1a\n")
    assert json.loads(bundle.read("manifest.json"))["version"] == arv_mcp.__version__


def test_bundle_ships_the_same_source_as_the_package(bundle):
    for path in (ROOT / "src" / "arv_mcp").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            arcname = path.relative_to(ROOT).as_posix()
            assert bundle.read(arcname) == path.read_bytes(), arcname


def test_build_is_reproducible(tmp_path):
    first = build_mcpb.build(tmp_path / "a", lock=False).read_bytes()
    second = build_mcpb.build(tmp_path / "b", lock=False).read_bytes()
    assert first == second
