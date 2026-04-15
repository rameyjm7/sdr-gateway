from __future__ import annotations

import asyncio
import csv
import io
import threading
import uuid
from collections import deque
from dataclasses import dataclass

from app.models import StreamConfig, SweepConfig
from app.sdr.backend import StreamRequest, SweepRequest
from app.sdr.registry import BackendRegistry


@dataclass
class StreamSession:
    id: str
    config: StreamConfig
    process: object
    status: str = "running"


class StreamManager:
    def __init__(self, registry: BackendRegistry) -> None:
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
    process: object
    status: str = "running"
    samples: deque = None
    _stop: threading.Event = None


class SweepManager:
    def __init__(self, registry: BackendRegistry) -> None:
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
        session._stop.set()
        backend = self._registry.backend_for_device(session.config.device_id)
        backend.stop_sweep(session.process)
        session.status = "stopped"
        del self._sessions[sweep_id]

    def recent_samples(self, sweep_id: str):
        return list(self._sessions[sweep_id].samples)

    def _collect_sweep_output(self, session: SweepSession) -> None:
        stdout = session.process.stdout
        if stdout is None:
            return

        while not session._stop.is_set():
            line = stdout.readline()
            if not line:
                if session.process.poll() is not None:
                    break
                continue

            parsed = self._parse_sweep_line(line)
            if parsed:
                session.samples.append(parsed)

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
