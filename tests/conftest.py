"""Shared test setup: unit tests must never touch the network.

Real HTTP is blocked unless a test is marked `live` (run with `-m live`).
"""
import pytest


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return

    def blocked(*a, **k):
        raise AssertionError("unit tests must not make network calls; mock data/client.py or mark the test live")

    monkeypatch.setattr("requests.post", blocked)
    monkeypatch.setattr("urllib.request.urlopen", blocked)


@pytest.fixture(autouse=True)
def _fresh_caches():
    from arv_mcp import service
    service.clear_caches()
    yield
    service.clear_caches()
