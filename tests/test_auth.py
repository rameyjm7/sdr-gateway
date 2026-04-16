from __future__ import annotations

import pytest

from app import auth
from app.config import get_settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_normalize_token_strips_noise():
    raw = "  'Bearer SDR_GATEWAY_API_TOKEN=abcDEF123+/=='  "
    assert auth._normalize_token(raw) == "abcDEF123+/=="


def test_normalize_token_extracts_base64_like_segment():
    raw = "token=***abcdefghijklmnopQRSTUV==***"
    assert auth._normalize_token(raw) == "abcdefghijklmnopQRSTUV=="


def test_token_validation_enabled(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_API_TOKEN", "secretToken123456")
    assert auth.auth_enabled() is True
    assert auth._token_valid("Bearer secretToken123456") is True
    assert auth._token_valid("wrong") is False


def test_token_validation_disabled(monkeypatch):
    monkeypatch.delenv("SDR_GATEWAY_API_TOKEN", raising=False)
    assert auth.auth_enabled() is False
    assert auth._token_valid(None) is True
