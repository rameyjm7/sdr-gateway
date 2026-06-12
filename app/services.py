from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
import os
import select
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from app.models import IQSweepConfig, StreamConfig, SweepConfig, TxBurstConfig
from app.sdr.backend import Device, IQSweepRequest, StreamRequest, SweepRequest, TxBurstRequest

logger = logging.getLogger(__name__)

DEFAULT_STREAM_CHUNK_BYTES = int(os.getenv("SDR_GATEWAY_STREAM_CHUNK_BYTES", str(64 * 1024)))


class ManagedProcess(Protocol):
    stdout: Any | None
    stderr: Any | None

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
    retuned_at: float = 0.0
    restart_count: int = 0
    restart_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class StreamManager:
    def __init__(self, registry: RegistryLike) -> None:
        self._registry = registry
        self._sessions: dict[str, StreamSession] = {}

    def list_states(self):
        return list(self._sessions.values())

    def get(self, stream_id: str) -> StreamSession:
        return self._sessions[stream_id]

    def start(self, config: StreamConfig) -> StreamSession:
        self._validate_stream_config(config)
        backend = self._registry.backend_for_device(config.device_id)
        process = backend.start_stream(self._stream_request(config))
        stream_id = str(uuid.uuid4())
        session = StreamSession(id=stream_id, config=config, process=process, retuned_at=time.time())
        self._sessions[stream_id] = session
        return session

    def retune(self, stream_id: str, config: StreamConfig) -> StreamSession:
        session = self._sessions[stream_id]
        if config.device_id != session.config.device_id:
            raise ValueError("retune must keep the same device_id")
        self._validate_stream_config(config)

        backend = self._registry.backend_for_device(config.device_id)
        session.status = "retuning"
        old_process = session.process
        backend.stop_stream(old_process)
        try:
            process = backend.start_stream(self._stream_request(config))
        except Exception:
            session.status = "stopped"
            raise
        session.process = process
        session.config = config
        session.status = "running"
        session.retuned_at = time.time()
        return session

    def stop(self, stream_id: str) -> None:
        session = self._sessions.pop(stream_id)
        backend = self._registry.backend_for_device(session.config.device_id)
        try:
            backend.stop_stream(session.process)
        finally:
            session.status = "stopped"

    def stop_all(self) -> None:
        for stream_id in list(self._sessions.keys()):
            try:
                self.stop(stream_id)
            except Exception:
                continue

    async def read_chunk(self, stream_id: str, nbytes: int = DEFAULT_STREAM_CHUNK_BYTES) -> bytes:
        retry_deadline = time.monotonic() + 1.5
        while True:
            session = self._sessions.get(stream_id)
            if session is None:
                return b""
            process = session.process
            stdout = process.stdout
            if stdout is None:
                return b""
            chunk = await asyncio.to_thread(stdout.read, nbytes)
            if self._sessions.get(stream_id) is not session:
                return b""
            if chunk:
                return chunk
            if session.status == "retuning" or (time.time() - session.retuned_at) < 1.0:
                if time.monotonic() < retry_deadline:
                    await asyncio.sleep(0.05)
                    continue
            if process.poll() is not None and self._is_continuous(session.config):
                restarted = await asyncio.to_thread(self._restart_stream, session, process)
                if restarted:
                    retry_deadline = time.monotonic() + 1.5
                    await asyncio.sleep(0.1)
                    continue
            return b""

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
                capture = _summarize_iq_capture(chunk)
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

    def _validate_stream_config(self, config: StreamConfig) -> None:
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

    def _stream_request(self, config: StreamConfig) -> StreamRequest:
        return StreamRequest(
            device_id=config.device_id,
            center_freq_hz=config.center_freq_hz,
            sample_rate_sps=config.sample_rate_sps,
            lna_gain_db=config.lna_gain_db,
            vga_gain_db=config.vga_gain_db,
            amp_enable=config.amp_enable,
            baseband_filter_hz=config.baseband_filter_hz,
            duration_seconds=config.duration_seconds,
            num_samples=config.num_samples,
        )

    @staticmethod
    def _is_continuous(config: StreamConfig) -> bool:
        return config.duration_seconds is None and config.num_samples is None

    def _restart_stream(self, session: StreamSession, failed_process: ManagedProcess) -> bool:
        with session.restart_lock:
            if self._sessions.get(session.id) is not session:
                return False
            if session.status != "running" or session.process is not failed_process:
                return session.status == "running"

            backend = self._registry.backend_for_device(session.config.device_id)
            returncode = failed_process.poll()
            stderr_tail = _read_stderr_tail(failed_process)
            try:
                backend.stop_stream(failed_process)
                new_process = backend.start_stream(self._stream_request(session.config))
            except Exception as exc:
                logger.exception(
                    "stream_process_restart_failed stream_id=%s device_id=%s returncode=%s "
                    "stderr_tail=%s error=%s",
                    session.id,
                    session.config.device_id,
                    returncode,
                    stderr_tail,
                    exc,
                )
                return False

            session.process = new_process
            session.restart_count += 1
            session.retuned_at = time.time()
            logger.warning(
                "stream_process_restarted stream_id=%s device_id=%s restart_count=%s "
                "returncode=%s stderr_tail=%s",
                session.id,
                session.config.device_id,
                session.restart_count,
                returncode,
                stderr_tail,
            )
            return True


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
class IQSweepSession:
    id: str
    stream_id: str
    config: IQSweepConfig
    centers_hz: list[int]
    process: ManagedProcess | None = None
    native: bool = False
    point_index: int = 0
    status: str = "running"
    retuned_at: float = 0.0
    native_buffer: bytes = b""

    @property
    def current_center_freq_hz(self) -> int:
        if not self.centers_hz:
            return 0
        return self.centers_hz[min(self.point_index, len(self.centers_hz) - 1)]


class IQSweepManager:
    def __init__(self, stream_manager: StreamManager) -> None:
        self._stream_manager = stream_manager
        self._sessions: dict[str, IQSweepSession] = {}

    def list_states(self):
        return list(self._sessions.values())

    def get(self, iq_sweep_id: str) -> IQSweepSession:
        return self._sessions[iq_sweep_id]

    def start(self, config: IQSweepConfig) -> IQSweepSession:
        centers_hz = self._centers_from_config(config)
        iq_sweep_id = str(uuid.uuid4())
        backend = self._stream_manager._registry.backend_for_device(config.device_id)  # noqa: SLF001
        if hasattr(backend, "start_iq_sweep"):
            process = backend.start_iq_sweep(
                IQSweepRequest(
                    device_id=config.device_id,
                    center_freqs_hz=centers_hz if config.center_freqs_hz else [],
                    start_freq_hz=config.start_freq_hz,
                    stop_freq_hz=config.stop_freq_hz,
                    hop_hz=config.hop_hz,
                    sample_rate_sps=config.sample_rate_sps,
                    dwell_s=config.dwell_s,
                    lna_gain_db=config.lna_gain_db,
                    vga_gain_db=config.vga_gain_db,
                    amp_enable=config.amp_enable,
                    baseband_filter_hz=config.baseband_filter_hz,
                    chunk_bytes=DEFAULT_STREAM_CHUNK_BYTES,
                )
            )
            session = IQSweepSession(
                id=iq_sweep_id,
                stream_id="native",
                config=config,
                centers_hz=centers_hz,
                process=process,
                native=True,
                retuned_at=time.time(),
            )
            self._sessions[iq_sweep_id] = session
            return session

        stream_config = self._stream_config(config, centers_hz[0])
        stream = self._stream_manager.start(stream_config)
        session = IQSweepSession(
            id=iq_sweep_id,
            stream_id=stream.id,
            config=config,
            centers_hz=centers_hz,
            retuned_at=time.time(),
        )
        self._sessions[iq_sweep_id] = session
        return session

    def stop(self, iq_sweep_id: str) -> None:
        session = self._sessions.pop(iq_sweep_id)
        if session.native and session.process is not None:
            backend = self._stream_manager._registry.backend_for_device(session.config.device_id)  # noqa: SLF001
            try:
                backend.stop_iq_sweep(session.process)
            finally:
                session.status = "stopped"
        else:
            try:
                self._stream_manager.stop(session.stream_id)
            finally:
                session.status = "stopped"

    def stop_all(self) -> None:
        for iq_sweep_id in list(self._sessions.keys()):
            try:
                self.stop(iq_sweep_id)
            except Exception:
                continue

    async def read_chunk(self, iq_sweep_id: str, nbytes: int = DEFAULT_STREAM_CHUNK_BYTES) -> dict[str, Any]:
        session = self._sessions[iq_sweep_id]
        if session.native:
            return await self._read_native_chunk(session)
        if time.time() - session.retuned_at >= float(session.config.dwell_s):
            self._advance(session)
        chunk = await self._stream_manager.read_chunk(session.stream_id, nbytes=nbytes)
        if not chunk:
            return self._chunk_payload(session, b"")
        return self._chunk_payload(session, chunk)

    async def _read_native_chunk(self, session: IQSweepSession) -> dict[str, Any]:
        process = session.process
        if process is None or process.stdout is None:
            return self._chunk_payload(session, b"")
        header = await asyncio.to_thread(self._read_native_exact, session, 40, 2.0)
        if not header:
            return self._chunk_payload(session, b"")
        if header[:8] != b"IQSWP1\x00\x00":
            raise ValueError("invalid native IQ sweep frame magic")
        header_len = int.from_bytes(header[8:12], "little")
        point_index = int.from_bytes(header[12:16], "little")
        center_freq_hz = int.from_bytes(header[16:24], "little")
        sample_rate_sps = int.from_bytes(header[24:28], "little")
        payload_len = int.from_bytes(header[28:32], "little")
        retuned_at = int.from_bytes(header[32:40], "little") / 1_000_000.0
        if header_len != 40:
            raise ValueError(f"unsupported native IQ sweep frame header length {header_len}")
        chunk = await asyncio.to_thread(self._read_native_exact, session, payload_len, 2.0)
        if chunk is None:
            session.native_buffer = header + session.native_buffer
            return self._chunk_payload(session, b"")
        session.point_index = point_index
        if center_freq_hz not in session.centers_hz:
            session.centers_hz.append(center_freq_hz)
        session.retuned_at = retuned_at
        return {
            "iq_sweep_id": session.id,
            "stream_id": session.stream_id,
            "device_id": session.config.device_id,
            "center_freq_hz": center_freq_hz,
            "sample_rate_sps": sample_rate_sps,
            "point_index": point_index,
            "retuned_at": retuned_at,
            "byte_count": len(chunk),
            "iq_i8_b64": base64.b64encode(chunk).decode("ascii"),
        }

    def _read_native_exact(self, session: IQSweepSession, size: int, timeout_s: float) -> bytes | None:
        process = session.process
        if process is None or process.stdout is None:
            return b""
        deadline = time.time() + max(0.01, timeout_s)
        fd = process.stdout.fileno()
        buffer = session.native_buffer
        while len(buffer) < size:
            returncode = process.poll()
            if returncode is not None:
                stderr_tail = self._native_stderr_tail(process)
                session.status = "error" if returncode else "stopped"
                raise RuntimeError(
                    f"native IQ sweep exited returncode={returncode}"
                    + (f" stderr_tail={stderr_tail}" if stderr_tail else "")
                )
            remaining = deadline - time.time()
            if remaining <= 0:
                session.native_buffer = buffer
                return None
            readable, _, _ = select.select([fd], [], [], min(0.1, remaining))
            if not readable:
                continue
            try:
                data = os.read(fd, max(size - len(buffer), 65536))
            except BlockingIOError:
                continue
            if not data:
                continue
            buffer += data
        out = buffer[:size]
        session.native_buffer = buffer[size:]
        return out

    @staticmethod
    def _native_stderr_tail(process: ManagedProcess, limit: int = 4000) -> str:
        if process.stderr is None:
            return ""
        try:
            fd = process.stderr.fileno()
            chunks: list[bytes] = []
            while True:
                readable, _, _ = select.select([fd], [], [], 0)
                if not readable:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception:
            return ""
        if not chunks:
            return ""
        return b"".join(chunks)[-limit:].decode("utf-8", errors="replace").strip()

    def _advance(self, session: IQSweepSession) -> None:
        if len(session.centers_hz) <= 1:
            session.retuned_at = time.time()
            return
        session.point_index = (session.point_index + 1) % len(session.centers_hz)
        center_freq_hz = session.current_center_freq_hz
        session.status = "retuning"
        self._stream_manager.retune(session.stream_id, self._stream_config(session.config, center_freq_hz))
        session.status = "running"
        session.retuned_at = time.time()

    def _chunk_payload(self, session: IQSweepSession, chunk: bytes) -> dict[str, Any]:
        return {
            "iq_sweep_id": session.id,
            "stream_id": session.stream_id,
            "device_id": session.config.device_id,
            "center_freq_hz": session.current_center_freq_hz,
            "sample_rate_sps": session.config.sample_rate_sps,
            "point_index": session.point_index,
            "retuned_at": session.retuned_at,
            "byte_count": len(chunk),
            "iq_i8_b64": base64.b64encode(chunk).decode("ascii"),
        }

    @staticmethod
    def _stream_config(config: IQSweepConfig, center_freq_hz: int) -> StreamConfig:
        return StreamConfig(
            device_id=config.device_id,
            center_freq_hz=int(center_freq_hz),
            sample_rate_sps=config.sample_rate_sps,
            lna_gain_db=config.lna_gain_db,
            vga_gain_db=config.vga_gain_db,
            amp_enable=config.amp_enable,
            baseband_filter_hz=config.baseband_filter_hz,
        )

    @staticmethod
    def _centers_from_config(config: IQSweepConfig) -> list[int]:
        if config.center_freqs_hz:
            centers = [int(freq) for freq in config.center_freqs_hz]
        else:
            if config.start_freq_hz is None or config.stop_freq_hz is None or config.hop_hz is None:
                raise ValueError("provide center_freqs_hz or start_freq_hz/stop_freq_hz/hop_hz")
            if config.start_freq_hz > config.stop_freq_hz:
                raise ValueError("start_freq_hz must be <= stop_freq_hz")
            centers = list(range(int(config.start_freq_hz), int(config.stop_freq_hz) + 1, int(config.hop_hz)))
        if not centers:
            raise ValueError("IQ sweep must include at least one center frequency")
        return centers


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


def _read_stderr_tail(process: ManagedProcess, limit: int = 1000) -> str:
    stderr = getattr(process, "stderr", None)
    if stderr is None or process.poll() is None:
        return ""
    try:
        raw = stderr.read()
    except (OSError, ValueError):
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")[-limit:].strip()
    return str(raw)[-limit:].strip()


def _summarize_iq_capture(chunk: bytes) -> dict[str, Any]:
    iq_i8 = np.frombuffer(chunk, dtype=np.int8)
    if iq_i8.size < 2:
        return {
            "_raw_bytes": bytes(chunk),
            "_fft_db": [],
            "bytes_read": len(chunk),
            "sample_pairs": 0,
            "mean_power_db": -255.0,
            "peak_power_db": -255.0,
            "peak_bin": 0,
        }
    if iq_i8.size % 2 != 0:
        iq_i8 = iq_i8[:-1]

    i = iq_i8[0::2].astype(np.float32)
    q = iq_i8[1::2].astype(np.float32)
    complex_iq = i + 1j * q
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
