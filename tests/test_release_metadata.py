"""Release metadata stays consistent: server.json, the README ownership
marker the MCP Registry checks on PyPI, and the package itself."""
import json
import re
import tomllib
from pathlib import Path

import arv_mcp

ROOT = Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_is_single_sourced():
    assert "version" not in PYPROJECT["project"]
    assert PYPROJECT["project"]["dynamic"] == ["version"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "src/arv_mcp/__init__.py"
    assert re.fullmatch(r"\d+\.\d+\.\d+", arv_mcp.__version__)


def test_readme_has_registry_ownership_marker():
    readme = (ROOT / PYPROJECT["project"]["readme"]).read_text(encoding="utf-8")
    assert f"mcp-name: {SERVER['name']}" in readme


def test_server_json_points_at_this_package():
    assert SERVER["name"] == "io.github.mariov7757/arv-mcp"
    assert SERVER["repository"]["url"] == PYPROJECT["project"]["urls"]["Repository"]
    (package,) = SERVER["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == PYPROJECT["project"]["name"]
    assert package["transport"] == {"type": "stdio"}
    assert package["version"] == SERVER["version"]


def test_server_json_registry_limits():
    assert 1 <= len(SERVER["description"]) <= 100
    assert 1 <= len(SERVER["title"]) <= 100
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", SERVER["name"])


def test_documented_env_vars_exist_in_code():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src").rglob("*.py"))
    for var in SERVER["packages"][0]["environmentVariables"]:
        assert var["name"] in src
