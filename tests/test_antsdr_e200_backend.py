from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from app.sdr.antsdr_e200_backend import AntSDRE200Backend

pytestmark = pytest.mark.unit


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self._returncode = returncode

    def poll(self):
        return self._returncode

    def wait(self, timeout: float | None = None):
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9


def test_list_devices_exposes_configured_device_even_without_adi():
    backend = AntSDRE200Backend()
    devices = backend.list_devices()
    assert len(devices) == 1
    assert devices[0].id == "antsdre200:0"
    assert devices[0].driver == "antsdre200"
    assert "192.168.1.10" in (devices[0].notes or "")


def test_start_stream_builds_libiio_worker_command(monkeypatch):
    backend = AntSDRE200Backend()
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.sdr.antsdr_e200_backend.Path.exists", lambda self: True)

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProcess(None)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    process = backend.start_stream(
        type(
            "Request",
            (),
            {
                "device_id": "antsdre200:0",
                "center_freq_hz": 915_000_000,
                "sample_rate_sps": 2_000_000,
                "lna_gain_db": 16,
                "vga_gain_db": 20,
                "amp_enable": False,
                "baseband_filter_hz": 2_000_000,
                "duration_seconds": 5,
                "num_samples": None,
            },
        )()
    )

    assert isinstance(process, _FakeProcess)
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "libiio_worker.py" in str(cmd[1])
    assert "--uri" in cmd
    assert "ip:192.168.1.10" in cmd
    assert "--mode" in cmd and "rx" in cmd


def test_start_tx_burst_writes_payload_and_tracks_tempfile(monkeypatch, tmp_path):
    backend = AntSDRE200Backend()
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.sdr.antsdr_e200_backend.Path.exists", lambda self: True)
    monkeypatch.setattr(
        "app.sdr.antsdr_e200_backend.tempfile.mkstemp",
        lambda prefix, suffix: (
            os.open(tmp_path / "payload.iq", os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600),
            str(tmp_path / "payload.iq"),
        ),
    )

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProcess(None)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    process = backend.start_tx_burst(
        type(
            "Request",
            (),
            {
                "device_id": "antsdre200:0",
                "center_freq_hz": 915_000_000,
                "sample_rate_sps": 2_000_000,
                "tx_gain_db": 20,
                "amp_enable": False,
                "baseband_filter_hz": 2_000_000,
                "iq_i8": b"\x01\x02\x03\x04",
                "repeat": 2,
                "timeout_seconds": 5,
            },
        )()
    )

    assert isinstance(process, _FakeProcess)
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--mode" in cmd and "tx" in cmd
    assert "--iq-file" in cmd
    iq_path = Path(cmd[cmd.index("--iq-file") + 1])
    assert iq_path.read_bytes() == b"\x01\x02\x03\x04"
