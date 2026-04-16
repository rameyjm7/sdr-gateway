from __future__ import annotations

from pydantic import BaseModel, Field


class OkResponse(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    detail: str


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
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "hackrf:0",
                    "center_freq_hz": 751000000,
                    "sample_rate_sps": 2000000,
                    "lna_gain_db": 16,
                    "vga_gain_db": 20,
                    "amp_enable": False,
                    "baseband_filter_hz": 2000000,
                    "duration_seconds": 5,
                }
            ]
        }
    }


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
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "hackrf:0",
                    "start_freq_hz": 700000000,
                    "stop_freq_hz": 820000000,
                    "bin_width_hz": 100000,
                    "lna_gain_db": 16,
                    "vga_gain_db": 20,
                    "amp_enable": False,
                }
            ]
        }
    }


class SweepState(BaseModel):
    sweep_id: str
    status: str
    config: SweepConfig


class SweepSample(BaseModel):
    timestamp: str
    hz_low: int
    hz_high: int
    db_values: list[float]


class TxBurstConfig(BaseModel):
    device_id: str = Field(description="Device identifier from /devices")
    center_freq_hz: int = Field(ge=1_000_000, le=6_000_000_000)
    sample_rate_sps: int = Field(ge=200_000, le=61_440_000)
    tx_gain_db: int = Field(default=20, ge=0, le=62)
    amp_enable: bool = False
    baseband_filter_hz: int | None = Field(default=None, ge=200_000, le=61_440_000)
    # Base64-encoded interleaved int8 IQ bytes (I,Q,I,Q...).
    iq_i8_b64: str = Field(min_length=4)
    repeat: int = Field(default=1, ge=1, le=1024)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "hackrf:0",
                    "center_freq_hz": 751000000,
                    "sample_rate_sps": 2000000,
                    "tx_gain_db": 30,
                    "amp_enable": False,
                    "baseband_filter_hz": 2000000,
                    "iq_i8_b64": "AQIDBA==",
                    "repeat": 16,
                    "timeout_seconds": 10,
                }
            ]
        }
    }


class TxState(BaseModel):
    tx_id: str
    status: str
    config: TxBurstConfig
    returncode: int | None = None
