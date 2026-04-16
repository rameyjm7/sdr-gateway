from __future__ import annotations

import pytest

from app import main
from app.config import get_settings

pytestmark = pytest.mark.unit
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_API_TOKEN", "test-token-for-metrics")
    monkeypatch.setenv("SDR_GATEWAY_METRICS_ENABLED", "1")
    get_settings.cache_clear()
    main.settings = get_settings()
    with TestClient(main.app) as c:
        yield c
    get_settings.cache_clear()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token-for-metrics"}


def test_metrics_requires_auth(client: TestClient):
    r = client.get("/metrics")
    assert r.status_code == 401


def test_metrics_has_counters(client: TestClient):
    # Make one authenticated request to create some counters.
    _ = client.get("/health")
    r = client.get("/metrics", headers=_auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert "uptime_seconds" in body
    assert "http_requests_total" in body
    assert "active_sessions" in body
    assert {"streams", "sweeps", "tx"} <= set(body["active_sessions"].keys())
    assert "x-request-id" in r.headers


def test_metrics_disabled_returns_404(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_API_TOKEN", "test-token-for-metrics")
    monkeypatch.setenv("SDR_GATEWAY_METRICS_ENABLED", "0")
    get_settings.cache_clear()
    main.settings = get_settings()
    with TestClient(main.app) as c:
        r = c.get("/metrics", headers=_auth_headers())
        assert r.status_code == 404
