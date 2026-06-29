from __future__ import annotations

import pytest

from app import main
from app.config import get_settings
from app.models import StreamConfig, TxBurstConfig

pytestmark = pytest.mark.unit
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


class _FakeSession:
    def __init__(self, sid: str, config) -> None:
        self.id = sid
        self.status = "running"
        self.config = config
        self.returncode = None


class _FakeStreamManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {}

    def start(self, config: StreamConfig):
        session = _FakeSession("stream-1", config)
        self._sessions[session.id] = session
        return session

    def stop(self, stream_id: str) -> None:
        if stream_id not in self._sessions:
            raise KeyError(stream_id)
        del self._sessions[stream_id]

    def list_states(self):
        return list(self._sessions.values())

    def get(self, stream_id: str):
        return self._sessions[stream_id]

    async def probe(self, config: StreamConfig, capture_count: int = 2, chunk_size: int = 16384):
        return {
            "stream_id": "stream-1",
            "status": "alive",
            "alive": True,
            "device_id": config.device_id,
            "capture_count": capture_count,
            "captures": [
                {
                    "capture_index": 1,
                    "bytes_read": chunk_size,
                    "sample_pairs": chunk_size // 2,
                    "mean_power_db": -12.5,
                    "peak_power_db": 3.0,
                    "peak_bin": 4,
                },
                {
                    "capture_index": 2,
                    "bytes_read": chunk_size,
                    "sample_pairs": chunk_size // 2,
                    "mean_power_db": -12.0,
                    "peak_power_db": 3.5,
                    "peak_bin": 5,
                },
            ],
            "comparison": {
                "same_bytes": False,
                "mean_abs_fft_delta_db": 0.5,
                "mean_power_delta_db": 0.5,
                "peak_power_delta_db": 0.5,
                "peak_bin_delta": 1,
            },
        }


class _FakeTxManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {}

    def start(self, config: TxBurstConfig):
        session = _FakeSession("tx-1", config)
        self._sessions[session.id] = session
        return session

    def stop(self, tx_id: str) -> None:
        if tx_id not in self._sessions:
            raise KeyError(tx_id)
        del self._sessions[tx_id]

    def list_states(self):
        return list(self._sessions.values())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_API_TOKEN", "test-token")
    get_settings.cache_clear()
    main.settings = get_settings()
    monkeypatch.setattr(main, "stream_manager", _FakeStreamManager())
    monkeypatch.setattr(main, "tx_manager", _FakeTxManager())
    with TestClient(main.app) as c:
        yield c
    get_settings.cache_clear()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_health_is_public(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_verify_auth_rejects_without_token(client: TestClient):
    r = client.get("/auth/verify")
    assert r.status_code == 401
    assert "detail" in r.json()


def test_verify_auth_accepts_with_token(client: TestClient):
    r = client.get("/auth/verify", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stream_start_and_stop(client: TestClient):
    payload = {
        "device_id": "hackrf:0",
        "center_freq_hz": 100_000_000,
        "sample_rate_sps": 2_000_000,
        "lna_gain_db": 16,
        "vga_gain_db": 20,
        "amp_enable": False,
    }
    started = client.post("/streams/start", headers=_auth_headers(), json=payload)
    assert started.status_code == 200
    assert started.json()["stream_id"] == "stream-1"

    stopped = client.post("/streams/stream-1/stop", headers=_auth_headers())
    assert stopped.status_code == 200
    assert stopped.json() == {"ok": True}


def test_stream_probe_reports_metric(client: TestClient):
    payload = {
        "device_id": "hackrf:0",
        "center_freq_hz": 100_000_000,
        "sample_rate_sps": 2_000_000,
        "lna_gain_db": 16,
        "vga_gain_db": 20,
        "amp_enable": False,
        "capture_count": 2,
        "chunk_size": 1024,
    }
    probed = client.post("/streams/probe", headers=_auth_headers(), json=payload)
    assert probed.status_code == 200
    body = probed.json()
    assert body["alive"] is True
    assert body["comparison"]["same_bytes"] is False
    assert len(body["captures"]) == 2


def test_tx_start_and_stop(client: TestClient):
    payload = {
        "device_id": "hackrf:0",
        "center_freq_hz": 751_000_000,
        "sample_rate_sps": 2_000_000,
        "tx_gain_db": 20,
        "amp_enable": False,
        "baseband_filter_hz": 2_000_000,
        "iq_i8_b64": "AQIDBA==",
        "repeat": 2,
        "timeout_seconds": 10,
    }
    started = client.post("/tx/start", headers=_auth_headers(), json=payload)
    assert started.status_code == 200
    assert started.json()["tx_id"] == "tx-1"

    stopped = client.post("/tx/tx-1/stop", headers=_auth_headers())
    assert stopped.status_code == 200
    assert stopped.json() == {"ok": True}
