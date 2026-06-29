from __future__ import annotations

import asyncio
import base64

import pytest

from app.models import StreamConfig, TxBurstConfig
from app.sdr.backend import Device
from app.services import StreamManager, TxManager

pytestmark = pytest.mark.unit


class _Proc:
    def __init__(self) -> None:
        self.stopped = False
        self.stdout = None

    def poll(self):
        return 0 if self.stopped else None


class _Backend:
    def __init__(self) -> None:
        self.stream_stops = 0
        self.tx_stops = 0

    def start_stream(self, _request):
        return _Proc()

    def stop_stream(self, process) -> None:
        process.stopped = True
        self.stream_stops += 1

    def start_tx_burst(self, _request):
        return _Proc()

    def stop_tx_burst(self, process) -> None:
        process.stopped = True
        self.tx_stops += 1


class _Registry:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.device = Device(
            id="hackrf:0",
            driver="hackrf",
            label="HackRF",
            serial=None,
            freq_min_hz=1_000_000,
            freq_max_hz=6_000_000_000,
            max_sample_rate_sps=20_000_000,
            notes=None,
        )

    def list_devices(self):
        return [self.device]

    def backend_for_device(self, _device_id: str):
        return self.backend


def _stream_config() -> StreamConfig:
    return StreamConfig(
        device_id="hackrf:0",
        center_freq_hz=100_000_000,
        sample_rate_sps=2_000_000,
        lna_gain_db=16,
        vga_gain_db=20,
        amp_enable=False,
    )


def _tx_config() -> TxBurstConfig:
    return TxBurstConfig(
        device_id="hackrf:0",
        center_freq_hz=100_000_000,
        sample_rate_sps=2_000_000,
        tx_gain_db=20,
        amp_enable=False,
        iq_i8_b64=base64.b64encode(b"\x01\x02\x03\x04").decode("ascii"),
        repeat=1,
        timeout_seconds=3,
    )


def test_stream_repeated_start_stop_cycles():
    backend = _Backend()
    manager = StreamManager(_Registry(backend))
    for _ in range(5):
        session = manager.start(_stream_config())
        manager.stop(session.id)
    assert backend.stream_stops == 5


def test_stop_all_cleans_up_streams_and_tx():
    backend = _Backend()
    registry = _Registry(backend)
    stream_manager = StreamManager(registry)
    tx_manager = TxManager(registry)

    s1 = stream_manager.start(_stream_config())
    s2 = stream_manager.start(_stream_config())
    t1 = tx_manager.start(_tx_config())

    assert stream_manager.get(s1.id)
    assert stream_manager.get(s2.id)
    assert tx_manager.get(t1.id)

    stream_manager.stop_all()
    tx_manager.stop_all()

    assert stream_manager.list_states() == []
    assert tx_manager.list_states() == []
    assert backend.stream_stops == 2
    assert backend.tx_stops == 1


def test_stream_probe_reads_two_distinct_captures(monkeypatch):
    backend = _Backend()
    manager = StreamManager(_Registry(backend))
    chunks = [
        bytes([i % 256 for i in range(64)]),
        bytes([(i * 3) % 256 for i in range(64)]),
    ]

    async def _fake_read_chunk(_stream_id: str, nbytes: int = 16384):
        assert nbytes == 64
        return chunks.pop(0)

    monkeypatch.setattr(manager, "read_chunk", _fake_read_chunk)

    result = asyncio.run(manager.probe(_stream_config(), capture_count=2, chunk_size=64))

    assert result["alive"] is True
    assert result["comparison"]["same_bytes"] is False
    assert result["comparison"]["mean_abs_fft_delta_db"] >= 0
    assert len(result["captures"]) == 2
    assert backend.stream_stops == 1
