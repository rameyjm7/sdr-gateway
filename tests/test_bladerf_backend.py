from __future__ import annotations

import pytest

from app.sdr.backend import StreamRequest
from app.sdr.bladerf_backend import BladeRFBackend

pytestmark = pytest.mark.unit


def _stream_request(**updates) -> StreamRequest:
    defaults = {
        "device_id": "bladerf:0",
        "center_freq_hz": 462_500_000,
        "sample_rate_sps": 2_000_000,
        "lna_gain_db": 32,
        "vga_gain_db": 32,
        "amp_enable": False,
        "baseband_filter_hz": 1_500_000,
        "rx_channels": [0, 1],
        "duration_seconds": None,
        "num_samples": None,
    }
    defaults.update(updates)
    return StreamRequest(**defaults)


def test_start_stream_passes_dual_rx_channels_to_soapy_worker(monkeypatch):
    popen_calls: list[list[str]] = []

    class _FakeProcess:
        def poll(self):
            return None

    def fake_popen(cmd, **_kwargs):
        popen_calls.append(cmd)
        return _FakeProcess()

    monkeypatch.setattr("app.sdr.bladerf_backend.subprocess.Popen", fake_popen)

    BladeRFBackend().start_stream(_stream_request())

    assert popen_calls
    cmd = popen_calls[0]
    assert "--rx-channels" in cmd
    assert cmd[cmd.index("--rx-channels") + 1] == "0,1"
