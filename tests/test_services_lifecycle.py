from __future__ import annotations

import asyncio
import base64
import io

import pytest

from app.models import IQSweepConfig, StreamConfig, TxBurstConfig
from app.sdr.backend import Device
from app.services import IQSweepManager, StreamManager, TxManager

pytestmark = pytest.mark.unit


class _Proc:
    def __init__(self, stdout: io.BytesIO | None = None, returncode: int | None = None) -> None:
        self.stopped = returncode is not None
        self.stdout = stdout
        self.stderr = io.BytesIO(b"transient SDR failure")
        self.returncode = returncode

    def poll(self):
        if self.stopped:
            return self.returncode if self.returncode is not None else 0
        return None


class _Backend:
    def __init__(self) -> None:
        self.stream_stops = 0
        self.stream_starts = 0
        self.tx_stops = 0
        self.stream_processes: list[_Proc] = []

    def start_stream(self, _request):
        self.stream_starts += 1
        if self.stream_processes:
            return self.stream_processes.pop(0)
        return _Proc()

    def stop_stream(self, process) -> None:
        process.stopped = True
        self.stream_stops += 1

    def start_tx_burst(self, _request):
        return _Proc()

    def stop_tx_burst(self, process) -> None:
        process.stopped = True
        self.tx_stops += 1


class _FailingRestartBackend(_Backend):
    def start_stream(self, _request):
        self.stream_starts += 1
        if self.stream_starts == 1:
            return _Proc(stdout=io.BytesIO(), returncode=1)
        raise RuntimeError("hackrf_transfer exited during stream startup")


class _RetuningBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.retune_requests = []

    def retune_stream(self, _process, request) -> bool:
        self.retune_requests.append(request)
        return True


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


def test_continuous_stream_retune_keeps_existing_process_open():
    backend = _RetuningBackend()
    manager = StreamManager(_Registry(backend))
    session = manager.start(_stream_config())
    original_process = session.process

    retuned = manager.retune(
        session.id,
        _stream_config().model_copy(update={"center_freq_hz": 101_100_000}),
    )

    assert retuned.process is original_process
    assert retuned.config.center_freq_hz == 101_100_000
    assert backend.stream_starts == 1
    assert backend.stream_stops == 0
    assert len(backend.retune_requests) == 1


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


def test_continuous_stream_restarts_after_subprocess_exit():
    backend = _Backend()
    backend.stream_processes = [
        _Proc(stdout=io.BytesIO(), returncode=1),
        _Proc(stdout=io.BytesIO(b"recovered IQ")),
    ]
    manager = StreamManager(_Registry(backend))
    session = manager.start(_stream_config())

    chunk = asyncio.run(manager.read_chunk(session.id, nbytes=64))

    assert chunk == b"recovered IQ"
    assert session.restart_count == 1
    assert backend.stream_starts == 2
    assert backend.stream_stops == 1


def test_failed_continuous_stream_restart_does_not_replace_session_process():
    backend = _FailingRestartBackend()
    manager = StreamManager(_Registry(backend))
    session = manager.start(_stream_config())
    original_process = session.process

    chunk = asyncio.run(manager.read_chunk(session.id, nbytes=64))

    assert chunk == b""
    assert session.process is original_process
    assert session.restart_count == 0
    assert backend.stream_starts == 2
    assert backend.stream_stops == 1


def test_read_chunk_after_stream_removed_returns_empty_chunk():
    backend = _Backend()
    manager = StreamManager(_Registry(backend))
    session = manager.start(_stream_config())
    manager.stop(session.id)

    chunk = asyncio.run(manager.read_chunk(session.id, nbytes=64))

    assert chunk == b""
    assert backend.stream_stops == 1


def test_stream_cursors_fan_out_same_buffered_iq_to_multiple_consumers():
    backend = _Backend()
    backend.stream_processes = [_Proc(stdout=io.BytesIO(b"shared IQ"), returncode=0)]
    manager = StreamManager(_Registry(backend))
    session = manager.start(_stream_config().model_copy(update={"num_samples": 1024}))
    first = manager.create_cursor(session.id, start="oldest")
    second = manager.create_cursor(session.id, start="oldest")

    first_chunk = asyncio.run(manager.read_chunk(session.id, nbytes=64, cursor_id=first))
    second_chunk = asyncio.run(manager.read_chunk(session.id, nbytes=64, cursor_id=second))

    assert first_chunk == b"shared IQ"
    assert second_chunk == b"shared IQ"


def test_finite_stream_does_not_restart_at_eof():
    backend = _Backend()
    backend.stream_processes = [_Proc(stdout=io.BytesIO(), returncode=0)]
    manager = StreamManager(_Registry(backend))
    config = _stream_config().model_copy(update={"num_samples": 1024})
    session = manager.start(config)

    chunk = asyncio.run(manager.read_chunk(session.id, nbytes=64))

    assert chunk == b""
    assert session.restart_count == 0
    assert backend.stream_starts == 1


def test_iq_sweep_returns_iq_with_frequency_metadata():
    backend = _Backend()
    backend.stream_processes = [_Proc(stdout=io.BytesIO(b"iq-2402"))]
    stream_manager = StreamManager(_Registry(backend))
    manager = IQSweepManager(stream_manager)
    session = manager.start(
        IQSweepConfig(
            device_id="hackrf:0",
            center_freqs_hz=[2_402_000_000, 2_426_000_000],
            sample_rate_sps=2_000_000,
            dwell_s=10.0,
            lna_gain_db=40,
            vga_gain_db=62,
        )
    )

    payload = asyncio.run(manager.read_chunk(session.id, nbytes=64))

    assert payload["center_freq_hz"] == 2_402_000_000
    assert payload["sample_rate_sps"] == 2_000_000
    assert base64.b64decode(payload["iq_i8_b64"]) == b"iq-2402"


def test_iq_sweep_retunes_after_dwell():
    backend = _Backend()
    backend.stream_processes = [
        _Proc(stdout=io.BytesIO(b"old")),
        _Proc(stdout=io.BytesIO(b"iq-2426")),
    ]
    stream_manager = StreamManager(_Registry(backend))
    manager = IQSweepManager(stream_manager)
    session = manager.start(
        IQSweepConfig(
            device_id="hackrf:0",
            center_freqs_hz=[2_402_000_000, 2_426_000_000],
            sample_rate_sps=2_000_000,
            dwell_s=0.05,
        )
    )
    session.retuned_at = 0.0

    payload = asyncio.run(manager.read_chunk(session.id, nbytes=64))

    assert payload["center_freq_hz"] == 2_426_000_000
    assert payload["point_index"] == 1
    assert base64.b64decode(payload["iq_i8_b64"]) == b"iq-2426"
    assert backend.stream_stops == 1
