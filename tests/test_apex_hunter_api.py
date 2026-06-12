from __future__ import annotations

import pytest

from app import main
from app.config import get_settings
from app.models import StreamConfig, SweepConfig, TxBurstConfig

pytestmark = pytest.mark.unit
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


class _FakeDevice:
    def __init__(self, device_id: str, label: str) -> None:
        self.id = device_id
        self.driver = device_id.split(":", 1)[0]
        self.label = label
        self.serial = None
        self.freq_min_hz = 1_000_000
        self.freq_max_hz = 6_000_000_000
        self.max_sample_rate_sps = 20_000_000
        self.notes = None


class _FakeRegistry:
    def list_devices(self):
        return [_FakeDevice("hackrf:0", "HackRF One"), _FakeDevice("bladerf:0", "bladeRF")]

    def backend_for_device(self, _device_id: str):
        raise RuntimeError("not used in apex tests")


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


class _FakeSweepManager:
    def __init__(self) -> None:
        self.started: list[object] = []

    def start(self, config: SweepConfig):
        self.started.append(config)
        return _FakeSession("sweep-1", config)

    def stop(self, _sweep_id: str) -> None:
        return None

    def list_states(self):
        return []

    def recent_samples(self, _sweep_id: str):
        return []


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
    main.registry = _FakeRegistry()
    main.stream_manager = _FakeStreamManager()
    main.sweep_manager = _FakeSweepManager()
    main.tx_manager = _FakeTxManager()
    main.apex_hunter = main.ApexHunterService(main.registry, main.stream_manager, main.sweep_manager)
    with TestClient(main.app) as c:
        yield c
    get_settings.cache_clear()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_apex_resource_inventory_contains_sdr_devices(client: TestClient):
    resp = client.get("/apex/resources", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    ids = {r["id"] for r in body}
    assert "hackrf:0" in ids
    assert "bladerf:0" in ids


def test_planned_sweep_uses_hackrf_sweep_for_hackrf(client: TestClient):
    payload = {
        "device_id": "hackrf:0",
        "frequencies_hz": [2_402_000_000, 2_426_000_000, 2_480_000_000],
        "margin_hz": 2_000_000,
        "bin_width_hz": 100_000,
        "lna_gain_db": 40,
        "vga_gain_db": 62,
        "label": "ble-adv",
    }
    resp = client.post("/sweeps/plan/start", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "hackrf_sweep"
    assert body["label"] == "ble-adv"
    assert body["config"]["device_id"] == "hackrf:0"
    assert body["config"]["start_freq_hz"] == 2_400_000_000
    assert body["config"]["stop_freq_hz"] == 2_482_000_000
    assert body["config"]["bin_width_hz"] == 100_000
    assert body["config"]["vga_gain_db"] == 62


def test_apex_upsert_external_api_and_plan(client: TestClient):
    upsert = {
        "id": "sensor-grid:alpha",
        "kind": "api",
        "label": "Sensor Grid Alpha",
        "capabilities": ["status", "capture_job"],
        "metadata": {"base_url": "http://sensor-grid.local"},
    }
    added = client.post("/apex/resources", headers=_auth_headers(), json=upsert)
    assert added.status_code == 200
    assert added.json()["id"] == "sensor-grid:alpha"

    plan_req = {
        "objective": "SIGINT band hunt with external device coordination",
        "resources": ["hackrf:0", "sensor-grid:alpha"],
        "constraints": ["priority=unknown_emitters", "max_dwell=2s"],
        "execute": False,
        "max_actions": 4,
    }
    planned = client.post("/apex/plan", headers=_auth_headers(), json=plan_req)
    assert planned.status_code == 200
    payload = planned.json()
    assert payload["status"] == "planned"
    assert payload["actions"]
    targets = {a["target"] for a in payload["actions"]}
    assert "hackrf:0" in targets


def test_apex_run_executes_sweep_actions(client: TestClient):
    run_req = {
        "objective": "COMMINT occupancy scan",
        "resources": ["hackrf:0"],
        "execute": True,
        "max_actions": 2,
    }
    run_resp = client.post("/apex/run", headers=_auth_headers(), json=run_req)
    assert run_resp.status_code == 200
    body = run_resp.json()
    assert body["status"] == "running"
    assert any("Started sweep" in note for note in body["notes"])


def test_apex_templates_and_constraint_driven_plan(client: TestClient):
    templates = client.get("/apex/templates", headers=_auth_headers())
    assert templates.status_code == 200
    template_ids = {t["id"] for t in templates.json()}
    assert "sigint_wide_hunt" in template_ids
    assert "ew_spectrum_pressure" in template_ids

    plan_req = {
        "objective": "EW pressure mapping",
        "resources": ["hackrf:0"],
        "constraints": [
            "start_freq_hz=751000000",
            "stop_freq_hz=761000000",
            "bin_width_hz=25000",
        ],
        "execute": False,
        "max_actions": 1,
    }
    planned = client.post("/apex/plan", headers=_auth_headers(), json=plan_req)
    assert planned.status_code == 200
    action = planned.json()["actions"][0]
    assert action["tool"] == "start_sweep"
    assert action["params"]["start_freq_hz"] == 751000000
    assert action["params"]["stop_freq_hz"] == 761000000
    assert action["params"]["bin_width_hz"] == 25000
