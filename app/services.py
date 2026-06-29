from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
import queue
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Any, Protocol

import numpy as np

from app.models import StreamConfig, SweepConfig, TxBurstConfig
from app.sdr.backend import Device, StreamRequest, SweepRequest, TxBurstRequest

logger = logging.getLogger(__name__)


class ManagedProcess(Protocol):
    stdout: Any | None

    def poll(self) -> int | None: ...


class RegistryLike(Protocol):
    def list_devices(self) -> list[Device]: ...

    def backend_for_device(self, device_id: str) -> Any: ...


@dataclass
class StreamSession:
    id: str
    config: StreamConfig
    process: ManagedProcess
    status: str = "running"
    chunk_size: int = 16384
    fanout_stop: threading.Event | None = None
    fanout_thread: threading.Thread | None = None
    subscribers: dict[str, queue.Queue[bytes | None]] | None = None
    subscribers_lock: threading.Lock | None = None


class StreamManager:
    def __init__(self, registry: RegistryLike) -> None:
        self._registry = registry
        self._sessions: dict[str, StreamSession] = {}

    def list_states(self):
        return list(self._sessions.values())

    def get(self, stream_id: str) -> StreamSession:
        return self._sessions[stream_id]

    def start(self, config: StreamConfig) -> StreamSession:
        device = next((d for d in self._registry.list_devices() if d.id == config.device_id), None)
        if device is None:
            raise KeyError(f"Unknown device_id '{config.device_id}'")
        if not (device.freq_min_hz <= config.center_freq_hz <= device.freq_max_hz):
            raise ValueError(
                f"center_freq_hz {config.center_freq_hz} outside device range "
                f"[{device.freq_min_hz}, {device.freq_max_hz}]"
            )
        if config.sample_rate_sps > device.max_sample_rate_sps:
            raise ValueError(
                f"sample_rate_sps {config.sample_rate_sps} exceeds device max "
                f"{device.max_sample_rate_sps}"
            )

        stream_config = _resolve_stream_format(config, device)
        backend = self._registry.backend_for_device(config.device_id)
        process = backend.start_stream(
            StreamRequest(
                device_id=stream_config.device_id,
                center_freq_hz=stream_config.center_freq_hz,
                sample_rate_sps=stream_config.sample_rate_sps,
                lna_gain_db=stream_config.lna_gain_db,
                vga_gain_db=stream_config.vga_gain_db,
                amp_enable=stream_config.amp_enable,
                baseband_filter_hz=stream_config.baseband_filter_hz,
                iq_format=stream_config.iq_format,
                duration_seconds=stream_config.duration_seconds,
                num_samples=stream_config.num_samples,
            )
        )
        stream_id = str(uuid.uuid4())
        session = StreamSession(
            id=stream_id,
            config=stream_config,
            process=process,
            chunk_size=_stream_chunk_size(stream_config.iq_format),
            fanout_stop=threading.Event(),
            subscribers={},
            subscribers_lock=threading.Lock(),
        )
        session.fanout_thread = threading.Thread(target=self._fanout_stdout, args=(session,), daemon=True)
        self._sessions[stream_id] = session
        session.fanout_thread.start()
        return session

    def stop(self, stream_id: str) -> None:
        session = self._sessions[stream_id]
        if session.fanout_stop is not None:
            session.fanout_stop.set()
        if session.subscribers_lock is not None and session.subscribers is not None:
            with session.subscribers_lock:
                for subscriber in session.subscribers.values():
                    _queue_put_latest(subscriber, None)
                session.subscribers.clear()
        backend = self._registry.backend_for_device(session.config.device_id)
        backend.stop_stream(session.process)
        if (
            session.fanout_thread is not None
            and session.fanout_thread.is_alive()
            and session.fanout_thread is not threading.current_thread()
        ):
            session.fanout_thread.join(timeout=1.0)
        session.status = "stopped"
        del self._sessions[stream_id]

    def stop_all(self) -> None:
        for stream_id in list(self._sessions.keys()):
            try:
                self.stop(stream_id)
            except Exception:
                continue

    async def read_chunk(self, stream_id: str, nbytes: int = 16384) -> bytes:
        with self.subscribe(stream_id, max_chunks=4) as chunks:
            chunk = await asyncio.to_thread(chunks.get)
        return b"" if chunk is None else chunk

    @contextmanager
    def subscribe(self, stream_id: str, max_chunks: int = 16):
        session = self._sessions[stream_id]
        subscribers = session.subscribers
        lock = session.subscribers_lock
        if subscribers is None or lock is None:
            raise RuntimeError(f"Stream {stream_id} is not subscribable")

        subscriber_id = str(uuid.uuid4())
        chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=max(1, int(max_chunks)))
        with lock:
            subscribers[subscriber_id] = chunks
        try:
            yield chunks
        finally:
            with lock:
                subscribers.pop(subscriber_id, None)

    def _fanout_stdout(self, session: StreamSession) -> None:
        stdout = session.process.stdout
        if stdout is None:
            return
        stop = session.fanout_stop or threading.Event()
        while not stop.is_set():
            try:
                chunk = stdout.read(session.chunk_size)
            except Exception:
                break
            if not chunk:
                break
            lock = session.subscribers_lock
            subscribers = session.subscribers
            if lock is None or subscribers is None:
                continue
            with lock:
                queues = list(subscribers.values())
            for chunks in queues:
                _queue_put_latest(chunks, chunk)

        lock = session.subscribers_lock
        subscribers = session.subscribers
        if lock is not None and subscribers is not None:
            with lock:
                queues = list(subscribers.values())
            for chunks in queues:
                _queue_put_latest(chunks, None)

    async def probe(self, config: StreamConfig, capture_count: int = 2, chunk_size: int = 16384) -> dict[str, Any]:
        session = self.start(config)
        capture_count = max(2, int(capture_count))
        chunk_size = max(1024, int(chunk_size))
        captures: list[dict[str, Any]] = []

        try:
            for capture_index in range(capture_count):
                chunk = await self.read_chunk(session.id, nbytes=chunk_size)
                if not chunk:
                    break
                capture = _summarize_iq_capture(chunk, session.config.iq_format)
                capture["capture_index"] = capture_index + 1
                captures.append(capture)
                print(
                    "stream_probe "
                    f"stream_id={session.id} capture={capture_index + 1} "
                    f"bytes={capture['bytes_read']} "
                    f"mean_power_db={capture['mean_power_db']:.2f} "
                    f"peak_power_db={capture['peak_power_db']:.2f} "
                    f"peak_bin={capture['peak_bin']}"
                )
                logger.info(
                    "stream_probe_capture",
                    extra={
                        "stream_id": session.id,
                        "capture_index": capture_index + 1,
                        "device_id": config.device_id,
                        "bytes_read": capture["bytes_read"],
                        "mean_power_db": capture["mean_power_db"],
                        "peak_power_db": capture["peak_power_db"],
                        "peak_bin": capture["peak_bin"],
                    },
                )

            if len(captures) >= 2:
                first = captures[0]
                second = captures[1]
                first_fft = first.get("_fft_db", [])
                second_fft = second.get("_fft_db", [])
                delta = 0.0
                if len(first_fft) == len(second_fft) and len(first_fft) > 0:
                    delta = float(np.mean(np.abs(np.asarray(second_fft) - np.asarray(first_fft))))
                same_bytes = bool(first.get("_raw_bytes") == second.get("_raw_bytes"))
                comparison = {
                    "same_bytes": same_bytes,
                    "mean_abs_fft_delta_db": round(delta, 6),
                    "mean_power_delta_db": round(float(second["mean_power_db"] - first["mean_power_db"]), 6),
                    "peak_power_delta_db": round(float(second["peak_power_db"] - first["peak_power_db"]), 6),
                    "peak_bin_delta": int(abs(int(second["peak_bin"]) - int(first["peak_bin"]))),
                }
            else:
                comparison = {
                    "same_bytes": False,
                    "mean_abs_fft_delta_db": 0.0,
                    "mean_power_delta_db": 0.0,
                    "peak_power_delta_db": 0.0,
                    "peak_bin_delta": 0,
                }

            alive = len(captures) >= 2 and not comparison["same_bytes"]
            status = "alive" if alive else "stale"
            summary = {
                "stream_id": session.id,
                "status": status,
                "alive": alive,
                "device_id": config.device_id,
                "capture_count": len(captures),
                "captures": [
                    {
                        key: value
                        for key, value in capture.items()
                        if key not in {"_fft_db", "_raw_bytes"}
                    }
                    for capture in captures
                ],
                "comparison": comparison,
            }
            print(
                "stream_probe_summary "
                f"stream_id={session.id} device_id={config.device_id} "
                f"alive={alive} capture_count={len(captures)} "
                f"same_bytes={comparison['same_bytes']} "
                f"mean_abs_fft_delta_db={comparison['mean_abs_fft_delta_db']:.6f}"
            )
            logger.info(
                "stream_probe_summary",
                extra={
                    "stream_id": session.id,
                    "device_id": config.device_id,
                    "alive": alive,
                    "capture_count": len(captures),
                    "same_bytes": comparison["same_bytes"],
                    "mean_abs_fft_delta_db": comparison["mean_abs_fft_delta_db"],
                },
            )
            return summary
        finally:
            if session.id in self._sessions:
                try:
                    self.stop(session.id)
                except KeyError:
                    pass


@dataclass
class SweepSession:
    id: str
    config: SweepConfig
    process: ManagedProcess
    status: str = "running"
    samples: deque[dict[str, Any]] | None = None
    _stop: threading.Event | None = None


class SweepManager:
    def __init__(self, registry: RegistryLike) -> None:
        self._registry = registry
        self._sessions: dict[str, SweepSession] = {}

    def list_states(self):
        return list(self._sessions.values())

    def get(self, sweep_id: str) -> SweepSession:
        return self._sessions[sweep_id]

    def start(self, config: SweepConfig) -> SweepSession:
        backend = self._registry.backend_for_device(config.device_id)
        process = backend.start_sweep(
            SweepRequest(
                device_id=config.device_id,
                start_freq_hz=config.start_freq_hz,
                stop_freq_hz=config.stop_freq_hz,
                bin_width_hz=config.bin_width_hz,
                lna_gain_db=config.lna_gain_db,
                vga_gain_db=config.vga_gain_db,
                amp_enable=config.amp_enable,
            )
        )
        sweep_id = str(uuid.uuid4())
        session = SweepSession(
            id=sweep_id,
            config=config,
            process=process,
            samples=deque(maxlen=200),
            _stop=threading.Event(),
        )
        self._sessions[sweep_id] = session

        thread = threading.Thread(target=self._collect_sweep_output, args=(session,), daemon=True)
        thread.start()
        return session

    def stop(self, sweep_id: str) -> None:
        session = self._sessions[sweep_id]
        if session._stop is not None:
            session._stop.set()
        backend = self._registry.backend_for_device(session.config.device_id)
        backend.stop_sweep(session.process)
        session.status = "stopped"
        del self._sessions[sweep_id]

    def stop_all(self) -> None:
        for sweep_id in list(self._sessions.keys()):
            try:
                self.stop(sweep_id)
            except Exception:
                continue

    def recent_samples(self, sweep_id: str):
        samples = self._sessions[sweep_id].samples
        return list(samples) if samples is not None else []

    def _collect_sweep_output(self, session: SweepSession) -> None:
        stdout = session.process.stdout
        if stdout is None:
            return
        stop_event = session._stop
        sample_buffer = session.samples
        if stop_event is None or sample_buffer is None:
            return

        while not stop_event.is_set():
            line = stdout.readline()
            if not line:
                if session.process.poll() is not None:
                    break
                continue

            parsed = self._parse_sweep_line(line)
            if parsed:
                sample_buffer.append(parsed)

    @staticmethod
    def _parse_sweep_line(line: str) -> dict | None:
        # Typical CSV row:
        # date,time,hz_low,hz_high,hz_bin_width,num_samples,dB,dB,dB...
        try:
            row = next(csv.reader(io.StringIO(line)))
            if len(row) < 7:
                return None
            return {
                "timestamp": f"{row[0]}T{row[1]}Z",
                "hz_low": int(row[2]),
                "hz_high": int(row[3]),
                "db_values": [float(v) for v in row[6:]],
            }
        except Exception:
            return None


@dataclass
class TxSession:
    id: str
    config: TxBurstConfig
    process: ManagedProcess
    status: str = "running"
    returncode: int | None = None


class TxManager:
    def __init__(self, registry: RegistryLike) -> None:
        self._registry = registry
        self._sessions: dict[str, TxSession] = {}

    def _refresh(self) -> None:
        for session in self._sessions.values():
            if session.status == "running":
                rc = session.process.poll()
                if rc is not None:
                    session.status = "completed" if rc == 0 else "failed"
                    session.returncode = int(rc)

    def list_states(self):
        self._refresh()
        return list(self._sessions.values())

    def get(self, tx_id: str) -> TxSession:
        self._refresh()
        return self._sessions[tx_id]

    def start(self, config: TxBurstConfig) -> TxSession:
        device = next((d for d in self._registry.list_devices() if d.id == config.device_id), None)
        if device is None:
            raise KeyError(f"Unknown device_id '{config.device_id}'")
        if not (device.freq_min_hz <= config.center_freq_hz <= device.freq_max_hz):
            raise ValueError(
                f"center_freq_hz {config.center_freq_hz} outside device range "
                f"[{device.freq_min_hz}, {device.freq_max_hz}]"
            )
        if config.sample_rate_sps > device.max_sample_rate_sps:
            raise ValueError(
                f"sample_rate_sps {config.sample_rate_sps} exceeds device max "
                f"{device.max_sample_rate_sps}"
            )

        try:
            iq_i8 = base64.b64decode(config.iq_i8_b64.encode("ascii"), validate=True)
        except Exception as exc:
            raise ValueError("iq_i8_b64 must be valid base64") from exc
        if len(iq_i8) < 2:
            raise ValueError("iq_i8_b64 payload too small")
        if len(iq_i8) % 2 != 0:
            iq_i8 = iq_i8[:-1]

        backend = self._registry.backend_for_device(config.device_id)
        process = backend.start_tx_burst(
            TxBurstRequest(
                device_id=config.device_id,
                center_freq_hz=config.center_freq_hz,
                sample_rate_sps=config.sample_rate_sps,
                tx_gain_db=config.tx_gain_db,
                amp_enable=config.amp_enable,
                baseband_filter_hz=config.baseband_filter_hz,
                iq_i8=iq_i8,
                repeat=config.repeat,
                timeout_seconds=config.timeout_seconds,
            )
        )
        tx_id = str(uuid.uuid4())
        session = TxSession(id=tx_id, config=config, process=process)
        self._sessions[tx_id] = session
        return session

    def stop(self, tx_id: str) -> None:
        session = self._sessions[tx_id]
        backend = self._registry.backend_for_device(session.config.device_id)
        backend.stop_tx_burst(session.process)
        session.returncode = session.process.poll()
        session.status = "stopped"
        del self._sessions[tx_id]

    def stop_all(self) -> None:
        for tx_id in list(self._sessions.keys()):
            try:
                self.stop(tx_id)
            except Exception:
                continue


def _resolve_stream_format(config: StreamConfig, device: Device) -> StreamConfig:
    if config.iq_format != "native":
        return config

    driver = (device.driver or "").lower()
    iq_format = "i8" if driver in {"hackrf", "mock"} else "cs16"
    return config.model_copy(update={"iq_format": iq_format})


def _stream_chunk_size(iq_format: str) -> int:
    return 32768 if iq_format == "cs16" else 16384


def _queue_put_latest(chunks: queue.Queue[bytes | None], chunk: bytes | None) -> None:
    try:
        chunks.put_nowait(chunk)
        return
    except queue.Full:
        pass
    try:
        chunks.get_nowait()
        chunks.task_done()
    except queue.Empty:
        pass
    try:
        chunks.put_nowait(chunk)
    except queue.Full:
        pass


def _summarize_iq_capture(chunk: bytes, iq_format: str = "i8") -> dict[str, Any]:
    dtype = np.int16 if iq_format == "cs16" else np.int8
    iq_raw = np.frombuffer(chunk, dtype=dtype)
    if iq_raw.size < 2:
        return {
            "_raw_bytes": bytes(chunk),
            "_fft_db": [],
            "bytes_read": len(chunk),
            "sample_pairs": 0,
            "mean_power_db": -255.0,
            "peak_power_db": -255.0,
            "peak_bin": 0,
        }
    if iq_raw.size % 2 != 0:
        iq_raw = iq_raw[:-1]

    scale = 32768.0 if iq_format == "cs16" else 128.0
    i = iq_raw[0::2].astype(np.float32)
    q = iq_raw[1::2].astype(np.float32)
    complex_iq = (i + 1j * q) / scale
    fft_db = 20.0 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(complex_iq))) + 1e-12)
    peak_bin = int(np.argmax(fft_db)) if fft_db.size else 0
    return {
        "_raw_bytes": bytes(chunk),
        "_fft_db": fft_db.tolist(),
        "bytes_read": len(chunk),
        "sample_pairs": int(complex_iq.size),
        "mean_power_db": round(float(np.mean(fft_db)), 6),
        "peak_power_db": round(float(np.max(fft_db)), 6),
        "peak_bin": peak_bin,
    }
