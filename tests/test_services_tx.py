from __future__ import annotations

import base64

import pytest

from app.models import TxBurstConfig
from app.sdr.backend import Device
from app.services import TxManager

pytestmark = pytest.mark.unit


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self._returncode = returncode

    def poll(self):
        return self._returncode


class _FakeBackend:
    def __init__(self) -> None:
        self.last_request = None
        self.stopped: list[object] = []
        self.process = _FakeProcess(None)

    def start_tx_burst(self, request):
        self.last_request = request
        return self.process

    def stop_tx_burst(self, process) -> None:
        self.stopped.append(process)


class _FakeRegistry:
    def __init__(self, backend: _FakeBackend) -> None:
        self.backend = backend

    def list_devices(self):
        return [
            Device(
                id="hackrf:0",
                driver="hackrf",
                label="HackRF One",
                serial=None,
                freq_min_hz=1_000_000,
                freq_max_hz=6_000_000_000,
                max_sample_rate_sps=20_000_000,
                notes=None,
            )
        ]

    def backend_for_device(self, _device_id: str):
        return self.backend


def _tx_config(iq_bytes: bytes) -> TxBurstConfig:
    return TxBurstConfig(
        device_id="hackrf:0",
        center_freq_hz=915_000_000,
        sample_rate_sps=2_000_000,
        tx_gain_db=20,
        amp_enable=False,
        baseband_filter_hz=2_000_000,
        iq_i8_b64=base64.b64encode(iq_bytes).decode("ascii"),
        repeat=2,
        timeout_seconds=5,
    )


def test_start_tx_passes_decoded_payload_and_trims_odd_byte():
    backend = _FakeBackend()
    manager = TxManager(_FakeRegistry(backend))
    session = manager.start(_tx_config(b"\x01\x02\x03"))

    assert session.status == "running"
    assert backend.last_request is not None
    assert backend.last_request.iq_i8 == b"\x01\x02"
    assert backend.last_request.repeat == 2


def test_start_tx_rejects_invalid_base64():
    backend = _FakeBackend()
    manager = TxManager(_FakeRegistry(backend))
    bad_cfg = TxBurstConfig(
        device_id="hackrf:0",
        center_freq_hz=915_000_000,
        sample_rate_sps=2_000_000,
        tx_gain_db=20,
        amp_enable=False,
        baseband_filter_hz=2_000_000,
        iq_i8_b64="%%%%",
        repeat=1,
        timeout_seconds=5,
    )
    with pytest.raises(ValueError, match="valid base64"):
        manager.start(bad_cfg)


def test_list_states_refreshes_completed_status():
    backend = _FakeBackend()
    backend.process = _FakeProcess(0)
    manager = TxManager(_FakeRegistry(backend))
    session = manager.start(_tx_config(b"\x01\x02\x03\x04"))

    states = manager.list_states()
    assert len(states) == 1
    assert states[0].id == session.id
    assert states[0].status == "completed"
    assert states[0].returncode == 0


def test_stop_tx_stops_backend_and_removes_session():
    backend = _FakeBackend()
    backend.process = _FakeProcess(0)
    manager = TxManager(_FakeRegistry(backend))
    session = manager.start(_tx_config(b"\x01\x02\x03\x04"))

    manager.stop(session.id)
    assert backend.stopped == [backend.process]
    assert manager.list_states() == []
