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

    def stop_all(self):
        self._sessions.clear()


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

    def stop_all(self):
        self._sessions.clear()


class _FakeWiFiMonitorManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {}
        self._events = [
            {
                "seen_at": 1.0,
                "interface": "wlan0",
                "raw": "Beacon (TestNet) BSSID:aa:bb:cc:dd:ee:ff -42dBm",
                "kind": "beacon",
                "ssid": "TestNet",
                "bssid": "aa:bb:cc:dd:ee:ff",
                "source": None,
                "destination": None,
                "rssi_dbm": -42,
                "frequency_mhz": 2412,
                "channel": 1,
            }
        ]

    def list_interfaces(self):
        return [
            {
                "name": "wlan0",
                "type": "monitor",
                "mac": "00:11:22:33:44:55",
                "channel": 1,
                "frequency_mhz": 2412,
                "up": True,
            }
        ]

    def start(self, config):
        session = _FakeSession("wifi-1", config)
        session.event_count = 0
        self._sessions[session.id] = session
        return session

    def stop(self, wifi_scan_id: str) -> None:
        if wifi_scan_id not in self._sessions:
            raise KeyError(wifi_scan_id)
        del self._sessions[wifi_scan_id]

    def list_states(self):
        return list(self._sessions.values())

    def get(self, wifi_scan_id: str):
        return self._sessions[wifi_scan_id]

    def recent_events(self, wifi_scan_id: str, limit: int = 100):
        if wifi_scan_id not in self._sessions:
            raise KeyError(wifi_scan_id)
        return self._events[-limit:]

    def stop_all(self):
        self._sessions.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SDR_GATEWAY_API_TOKEN", "test-token-123456")
    get_settings.cache_clear()
    main.settings = get_settings()
    monkeypatch.setattr(main, "stream_manager", _FakeStreamManager())
    monkeypatch.setattr(main, "tx_manager", _FakeTxManager())
    monkeypatch.setattr(main, "wifi_monitor_manager", _FakeWiFiMonitorManager())
    with TestClient(main.app) as c:
        yield c
    get_settings.cache_clear()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token-123456"}


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
        "rx_channels": [0],
    }
    started = client.post("/streams/start", headers=_auth_headers(), json=payload)
    assert started.status_code == 200
    assert started.json()["stream_id"] == "stream-1"

    stopped = client.post("/streams/stream-1/stop", headers=_auth_headers())
    assert stopped.status_code == 200
    assert stopped.json() == {"ok": True}


def test_stream_start_accepts_dual_channel_bladerf_config(client: TestClient):
    payload = {
        "device_id": "bladerf:0",
        "center_freq_hz": 462_500_000,
        "sample_rate_sps": 2_000_000,
        "lna_gain_db": 32,
        "vga_gain_db": 32,
        "amp_enable": False,
        "baseband_filter_hz": 1_500_000,
        "rx_channels": [0, 1],
    }
    started = client.post("/streams/start", headers=_auth_headers(), json=payload)
    assert started.status_code == 200
    assert started.json()["config"]["rx_channels"] == [0, 1]


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


def test_wifi_monitor_interfaces_start_events_and_stop(client: TestClient):
    interfaces = client.get("/wifi/interfaces", headers=_auth_headers())
    assert interfaces.status_code == 200
    assert interfaces.json()[0]["name"] == "wlan0"
    assert interfaces.json()[0]["type"] == "monitor"

    started = client.post(
        "/wifi/scans/start",
        headers=_auth_headers(),
        json={"interface": "wlan0", "channel": 1, "set_channel": False, "set_monitor": False},
    )
    assert started.status_code == 200
    assert started.json()["wifi_scan_id"] == "wifi-1"

    scans = client.get("/wifi/scans", headers=_auth_headers())
    assert scans.status_code == 200
    assert scans.json()[0]["wifi_scan_id"] == "wifi-1"

    events = client.get("/wifi/scans/wifi-1/events", headers=_auth_headers())
    assert events.status_code == 200
    assert events.json()[0]["kind"] == "beacon"
    assert events.json()[0]["ssid"] == "TestNet"

    stopped = client.post("/wifi/scans/wifi-1/stop", headers=_auth_headers())
    assert stopped.status_code == 200
    assert stopped.json() == {"ok": True}
