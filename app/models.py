from __future__ import annotations

from pydantic import BaseModel, Field


class DeviceInfo(BaseModel):
    id: str
    driver: str
    label: str
    serial: str | None = None
    freq_min_hz: int
    freq_max_hz: int
    max_sample_rate_sps: int
    notes: str | None = None


class StreamConfig(BaseModel):
    device_id: str = Field(description="Device identifier from /devices")
    center_freq_hz: int = Field(ge=1_000_000, le=6_000_000_000)
    sample_rate_sps: int = Field(ge=200_000, le=61_440_000)
    lna_gain_db: int = Field(default=16, ge=0, le=40)
    vga_gain_db: int = Field(default=20, ge=0, le=62)
    amp_enable: bool = False
    baseband_filter_hz: int | None = Field(default=None, ge=200_000, le=61_440_000)
    # Optional finite capture controls. If set, backend may stop after N samples.
    duration_seconds: int | None = Field(default=None, ge=1, le=3600)
    num_samples: int | None = Field(default=None, ge=1)


class StreamState(BaseModel):
    stream_id: str
    status: str
    config: StreamConfig


class SweepConfig(BaseModel):
    device_id: str
    start_freq_hz: int = Field(ge=1_000_000, le=6_000_000_000)
    stop_freq_hz: int = Field(ge=1_000_000, le=6_000_000_000)
    bin_width_hz: int = Field(default=100_000, ge=2_445, le=5_000_000)
    lna_gain_db: int = Field(default=16, ge=0, le=40)
    vga_gain_db: int = Field(default=20, ge=0, le=62)
    amp_enable: bool = False


class SweepState(BaseModel):
    sweep_id: str
    status: str
    config: SweepConfig


class SweepSample(BaseModel):
    timestamp: str
    hz_low: int
    hz_high: int
    db_values: list[float]
