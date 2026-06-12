from __future__ import annotations

import importlib
import io
import sys
import types

import numpy as np
import pytest

pytestmark = pytest.mark.unit


class _FakeStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


class _FakeSdr:
    def __init__(self) -> None:
        self.rx_enabled_channels = []
        self.rx_buffer_size = 0
        self.sample_rate = 0.0
        self.rx_lo = 0.0
        self.rx_rf_bandwidth = 0.0
        self.gain_control_mode_chan0 = ""
        self.rx_hardwaregain_chan0 = 0.0
        self.rx_calls = 0

    def rx(self):
        self.rx_calls += 1
        base = float(self.rx_calls)
        return np.array([complex(base, -base), complex(base + 1, -(base + 1))], dtype=np.complex64)

    def rx_destroy_buffer(self):
        return None


def test_libiio_worker_sets_rx_buffer_and_discards_warmup(monkeypatch):
    fake_sdr = _FakeSdr()
    fake_adi = types.SimpleNamespace(ad9361=lambda uri: fake_sdr)
    monkeypatch.setitem(sys.modules, "adi", fake_adi)

    worker = importlib.import_module("app.sdr.libiio_worker")
    worker = importlib.reload(worker)

    args = types.SimpleNamespace(
        mode="rx",
        uri="ip:192.168.1.10",
        device_index=0,
        center_freq_hz=915_000_000,
        sample_rate_sps=2_000_000,
        baseband_filter_hz=2_000_000,
        lna_gain_db=16,
        vga_gain_db=20,
        duration_seconds=0,
        num_samples=2,
        rx_buffer_size=8192,
        warmup_reads=2,
        tx_gain_db=20,
        iq_file="",
        repeat=1,
        timeout_seconds=5,
    )

    monkeypatch.setattr(worker, "_parse_args", lambda: args)
    monkeypatch.setattr(worker.sys, "stdout", _FakeStdout())

    exit_code = worker._run_rx(fake_sdr, args)

    assert exit_code == 0
    assert fake_sdr.rx_buffer_size == 8192
    assert fake_sdr.rx_calls == 3
    assert worker.sys.stdout.buffer.getvalue()
