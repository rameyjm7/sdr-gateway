from __future__ import annotations

import io

import pytest

from app.sdr import hackrf_backend
from app.sdr.backend import IQSweepRequest, StreamRequest
from app.sdr.hackrf_backend import HackRFBackend

pytestmark = pytest.mark.unit


class _FakeProcess:
    def __init__(self, returncode: int | None, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO(stderr)
        self.pid = id(self) % 100000

    def poll(self):
        return self.returncode


def _stream_request(**updates) -> StreamRequest:
    defaults = {
        "device_id": "hackrf:0",
        "center_freq_hz": 2_475_000_000,
        "sample_rate_sps": 8_000_000,
        "lna_gain_db": 16,
        "vga_gain_db": 32,
        "amp_enable": False,
        "baseband_filter_hz": 6_000_000,
        "rx_channels": [0],
        "duration_seconds": None,
        "num_samples": None,
    }
    defaults.update(updates)
    return StreamRequest(**defaults)


def _iq_sweep_request(**updates) -> IQSweepRequest:
    defaults = {
        "device_id": "hackrf:0",
        "center_freqs_hz": [2_402_000_000, 2_426_000_000, 2_480_000_000],
        "start_freq_hz": None,
        "stop_freq_hz": None,
        "hop_hz": 2_000_000,
        "sample_rate_sps": 2_000_000,
        "dwell_s": 1.5,
        "lna_gain_db": 40,
        "vga_gain_db": 62,
        "amp_enable": False,
        "baseband_filter_hz": 2_000_000,
        "chunk_bytes": 65_536,
    }
    defaults.update(updates)
    return IQSweepRequest(**defaults)


def test_continuous_stream_recovers_gateway_owned_busy_child(monkeypatch):
    processes = [
        _FakeProcess(
            returncode=1,
            stderr=b"hackrf_open() failed: Resource busy (-1000)",
        ),
        _FakeProcess(returncode=None),
    ]
    popen_calls = []
    cleanup_calls = []

    def fake_popen(cmd):
        popen_calls.append(cmd)
        return processes.pop(0)

    monkeypatch.setattr(hackrf_backend, "_cmd_available", lambda _command: True)
    monkeypatch.setattr(hackrf_backend.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(hackrf_backend, "_popen_iq_stream", fake_popen)
    monkeypatch.setattr(hackrf_backend.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        hackrf_backend,
        "_terminate_child_hackrf_transfers",
        lambda exclude_pid=None: cleanup_calls.append(exclude_pid) or 1,
    )

    process = HackRFBackend().start_stream(_stream_request())

    assert process.poll() is None
    assert len(popen_calls) == 2
    assert cleanup_calls


def test_finite_stream_does_not_wait_for_startup_probe(monkeypatch):
    popen_calls = []
    sleep_calls = []

    def fake_popen(cmd):
        popen_calls.append(cmd)
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(hackrf_backend, "_cmd_available", lambda _command: True)
    monkeypatch.setattr(hackrf_backend.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(hackrf_backend, "_popen_iq_stream", fake_popen)
    monkeypatch.setattr(hackrf_backend.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    process = HackRFBackend().start_stream(_stream_request(num_samples=1024))

    assert process.poll() == 0
    assert len(popen_calls) == 1
    assert sleep_calls == []


def test_stream_prefers_native_persistent_worker_when_built(monkeypatch):
    popen_calls = []

    def fake_popen(cmd, **_kwargs):
        popen_calls.append((cmd, _kwargs))
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(
        hackrf_backend.os.path,
        "exists",
        lambda path: path == hackrf_backend.HACKRF_STREAM_BIN,
    )
    monkeypatch.setattr(hackrf_backend.subprocess, "Popen", fake_popen)

    process = HackRFBackend().start_stream(_stream_request())

    assert process.poll() is None
    cmd, kwargs = popen_calls[0]
    assert cmd[0].endswith("hackrf_stream")
    assert "--center-freq-hz" in cmd
    assert "--sample-rate-sps" in cmd
    assert kwargs["stdin"] == hackrf_backend.subprocess.PIPE
    assert HackRFBackend().retune_stream(process, _stream_request(center_freq_hz=101_100_000))
    assert b"101100000" in process._process.stdin.getvalue()


def test_iq_sweep_uses_native_worker(monkeypatch):
    popen_calls = []

    def fake_popen(cmd, **_kwargs):
        popen_calls.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(hackrf_backend.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(hackrf_backend.subprocess, "Popen", fake_popen)

    process = HackRFBackend().start_iq_sweep(_iq_sweep_request())

    assert process.poll() is None
    cmd = popen_calls[0]
    assert cmd[0].endswith("hackrf_iq_sweep")
    assert "--freqs" in cmd
    assert "2402000000,2426000000,2480000000" in cmd
    assert "--sample-rate-sps" in cmd
    assert "--chunk-bytes" in cmd
