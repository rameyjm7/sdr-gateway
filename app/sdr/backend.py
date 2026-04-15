from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Device:
    id: str
    driver: str
    label: str
    serial: str | None
    freq_min_hz: int
    freq_max_hz: int
    max_sample_rate_sps: int
    notes: str | None = None


@dataclass(frozen=True)
class StreamRequest:
    device_id: str
    center_freq_hz: int
    sample_rate_sps: int
    lna_gain_db: int
    vga_gain_db: int
    amp_enable: bool
    baseband_filter_hz: int | None
    duration_seconds: int | None
    num_samples: int | None


@dataclass(frozen=True)
class SweepRequest:
    device_id: str
    start_freq_hz: int
    stop_freq_hz: int
    bin_width_hz: int
    lna_gain_db: int
    vga_gain_db: int
    amp_enable: bool


class SDRBackend(Protocol):
    def list_devices(self) -> list[Device]: ...

    def start_stream(self, request: StreamRequest): ...

    def stop_stream(self, process) -> None: ...

    def start_sweep(self, request: SweepRequest): ...

    def stop_sweep(self, process) -> None: ...
