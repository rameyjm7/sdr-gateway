from __future__ import annotations

import pytest

from app.sdr.backend import StreamRequest
from app.sdr.sdrplay_backend import SDRplayBackend

pytestmark = pytest.mark.unit


def _stream_request(device_id: str = "sdrplay:0") -> StreamRequest:
    return StreamRequest(
        device_id=device_id,
        center_freq_hz=100_100_000,
        sample_rate_sps=2_000_000,
        lna_gain_db=4,
        vga_gain_db=40,
        amp_enable=False,
        baseband_filter_hz=2_000_000,
        rx_channels=[0],
        duration_seconds=None,
        num_samples=None,
    )


def test_list_devices_uses_soapy_driver_probe(monkeypatch):
    monkeypatch.setattr(
        "app.sdr.sdrplay_backend.find_driver_devices",
        lambda driver: [
            {
                "driver": driver,
                "label": "SDRplay Dev0 RSP2 1710022B20",
                "serial": "1710022B20",
            }
        ],
    )

    devices = SDRplayBackend().list_devices()

    assert len(devices) == 1
    assert devices[0].id == "sdrplay:0"
    assert devices[0].driver == "sdrplay"
    assert devices[0].serial == "1710022B20"
    assert "RSP2" in devices[0].label


def test_list_devices_falls_back_to_usb_presence(monkeypatch):
    monkeypatch.setattr("app.sdr.sdrplay_backend.find_driver_devices", lambda _driver: [])
    monkeypatch.setattr(
        "app.sdr.sdrplay_backend.lsusb_devices",
        lambda: [("1df7:3010", "SDRplay RSP2/RSP2pro")],
    )

    devices = SDRplayBackend().list_devices()

    assert len(devices) == 1
    assert devices[0].id == "sdrplay:0"
    assert devices[0].driver == "sdrplay"
    assert devices[0].label == "SDRplay RSP2/RSP2pro"


def test_start_stream_uses_generic_soapy_worker(monkeypatch):
    popen_calls: list[list[str]] = []

    class _FakeProcess:
        def poll(self):
            return None

    def fake_popen(cmd, **_kwargs):
        popen_calls.append(cmd)
        return _FakeProcess()

    monkeypatch.setattr("app.sdr.sdrplay_backend.subprocess.Popen", fake_popen)

    SDRplayBackend().start_stream(_stream_request())

    assert popen_calls
    cmd = popen_calls[0]
    assert cmd[1].endswith("soapy_worker.py")
    assert "--driver" in cmd
    assert "sdrplay" in cmd
    assert "--device-index" in cmd
