from __future__ import annotations

import pytest

from app.config import get_settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_load(monkeypatch):
    monkeypatch.delenv("SDR_GATEWAY_LOG_LEVEL", raising=False)
    monkeypatch.delenv("SDR_GATEWAY_LOG_JSON", raising=False)
    monkeypatch.delenv("SDR_GATEWAY_METRICS_ENABLED", raising=False)
    monkeypatch.delenv("SDR_GATEWAY_API_TOKEN", raising=False)

    s = get_settings()
    assert s.log_level == "INFO"
    assert s.log_json is False
    assert s.metrics_enabled is True
    assert s.api_token == ""


def test_invalid_log_level_raises(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_LOG_LEVEL", "LOUD")
    with pytest.raises(ValueError, match="Invalid SDR_GATEWAY_LOG_LEVEL"):
        _ = get_settings()


def test_short_token_raises(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_API_TOKEN", "short")
    with pytest.raises(ValueError, match="unexpectedly short"):
        _ = get_settings()


def test_bool_env_parsing(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_LOG_JSON", "1")
    monkeypatch.setenv("SDR_GATEWAY_METRICS_ENABLED", "no")
    s = get_settings()
    assert s.log_json is True
    assert s.metrics_enabled is False
