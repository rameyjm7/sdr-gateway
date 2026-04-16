from __future__ import annotations

import asyncio
import base64
import csv
import io
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

from app.models import StreamConfig, SweepConfig, TxBurstConfig
from app.sdr.backend import Device, StreamRequest, SweepRequest, TxBurstRequest


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

        backend = self._registry.backend_for_device(config.device_id)
        process = backend.start_stream(
            StreamRequest(
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
        )
        stream_id = str(uuid.uuid4())
        session = StreamSession(id=stream_id, config=config, process=process)
        self._sessions[stream_id] = session
        return session

    def stop(self, stream_id: str) -> None:
        session = self._sessions[stream_id]
        backend = self._registry.backend_for_device(session.config.device_id)
        backend.stop_stream(session.process)
        session.status = "stopped"
        del self._sessions[stream_id]

    def stop_all(self) -> None:
        for stream_id in list(self._sessions.keys()):
            try:
                self.stop(stream_id)
            except Exception:
                continue

    async def read_chunk(self, stream_id: str, nbytes: int = 16384) -> bytes:
        session = self._sessions[stream_id]
        stdout = session.process.stdout
        if stdout is None:
            return b""
        return await asyncio.to_thread(stdout.read, nbytes)


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
