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
    occupied: bool = False
    occupied_by: str | None = None
    occupied_id: str | None = None


class StreamConfig(BaseModel):
    device_id: str = Field(description="Device identifier from /devices")
    center_freq_hz: int = Field(ge=1_000, le=6_000_000_000)
    sample_rate_sps: int = Field(ge=200_000, le=61_440_000)
    lna_gain_db: int = Field(default=16, ge=0, le=40)
    vga_gain_db: int = Field(default=20, ge=0, le=62)
    amp_enable: bool = False
    replace_existing: bool = False
    baseband_filter_hz: int | None = Field(default=None, ge=1_000, le=61_440_000)
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


class StreamProbeConfig(StreamConfig):
    capture_count: int = Field(default=2, ge=2, le=10)
    chunk_size: int = Field(default=16384, ge=1024, le=262144)


class StreamProbeCapture(BaseModel):
    capture_index: int
    bytes_read: int
    sample_pairs: int
    mean_power_db: float
    peak_power_db: float
    peak_bin: int


class StreamProbeComparison(BaseModel):
    same_bytes: bool
    mean_abs_fft_delta_db: float
    mean_power_delta_db: float
    peak_power_delta_db: float
    peak_bin_delta: int


class StreamProbeState(BaseModel):
    stream_id: str
    status: str
    alive: bool
    device_id: str
    capture_count: int
    captures: list[StreamProbeCapture]
    comparison: StreamProbeComparison


class StreamRetuneConfig(BaseModel):
    device_id: str = Field(description="Device identifier from /devices")
    center_freq_hz: int = Field(ge=1_000, le=6_000_000_000)
    sample_rate_sps: int = Field(ge=200_000, le=61_440_000)
    lna_gain_db: int = Field(default=16, ge=0, le=40)
    vga_gain_db: int = Field(default=20, ge=0, le=62)
    amp_enable: bool = False
    baseband_filter_hz: int | None = Field(default=None, ge=1_000, le=61_440_000)


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


class SweepPlanRequest(BaseModel):
    device_id: str
    start_freq_hz: int | None = Field(default=None, ge=1_000_000, le=6_000_000_000)
    stop_freq_hz: int | None = Field(default=None, ge=1_000_000, le=6_000_000_000)
    center_freq_hz: int | None = Field(default=None, ge=1_000_000, le=6_000_000_000)
    span_hz: int | None = Field(default=None, ge=1, le=6_000_000_000)
    frequencies_hz: list[int] = Field(default_factory=list)
    margin_hz: int = Field(default=2_000_000, ge=0, le=200_000_000)
    bin_width_hz: int = Field(default=100_000, ge=2_445, le=5_000_000)
    lna_gain_db: int = Field(default=16, ge=0, le=40)
    vga_gain_db: int = Field(default=20, ge=0, le=62)
    amp_enable: bool = False
    strategy: str = Field(default="auto", pattern="^(auto|native_sweep)$")
    label: str | None = None
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "hackrf:0",
                    "frequencies_hz": [2402000000, 2426000000, 2480000000],
                    "margin_hz": 2_000_000,
                    "bin_width_hz": 100_000,
                    "lna_gain_db": 40,
                    "vga_gain_db": 40,
                    "strategy": "auto",
                    "label": "ble-advertising",
                }
            ]
        }
    }


class SweepState(BaseModel):
    sweep_id: str
    status: str
    config: SweepConfig


class SweepPlanState(BaseModel):
    sweep_id: str
    status: str
    engine: str
    config: SweepConfig
    label: str | None = None


class SweepSample(BaseModel):
    timestamp: str
    hz_low: int
    hz_high: int
    db_values: list[float]


class IQSweepConfig(BaseModel):
    device_id: str = Field(description="Device identifier from /devices")
    center_freqs_hz: list[int] = Field(default_factory=list, description="Explicit center frequencies to hop")
    start_freq_hz: int | None = Field(default=None, ge=1_000_000, le=6_000_000_000)
    stop_freq_hz: int | None = Field(default=None, ge=1_000_000, le=6_000_000_000)
    hop_hz: int | None = Field(default=None, ge=1)
    sample_rate_sps: int = Field(default=2_000_000, ge=200_000, le=61_440_000)
    dwell_s: float = Field(default=1.5, ge=0.05, le=60.0)
    lna_gain_db: int = Field(default=16, ge=0, le=40)
    vga_gain_db: int = Field(default=20, ge=0, le=62)
    amp_enable: bool = False
    baseband_filter_hz: int | None = Field(default=None, ge=200_000, le=61_440_000)
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "hackrf:0",
                    "center_freqs_hz": [2402000000, 2426000000, 2480000000],
                    "sample_rate_sps": 2000000,
                    "dwell_s": 1.5,
                    "lna_gain_db": 40,
                    "vga_gain_db": 62,
                    "baseband_filter_hz": 2000000,
                },
                {
                    "device_id": "hackrf:0",
                    "start_freq_hz": 2402000000,
                    "stop_freq_hz": 2480000000,
                    "hop_hz": 24000000,
                    "sample_rate_sps": 2000000,
                    "dwell_s": 1.5,
                },
            ]
        }
    }


class IQSweepState(BaseModel):
    iq_sweep_id: str
    stream_id: str
    status: str
    config: IQSweepConfig
    current_center_freq_hz: int
    point_index: int


class IQSweepChunk(BaseModel):
    iq_sweep_id: str
    stream_id: str
    device_id: str
    center_freq_hz: int
    sample_rate_sps: int
    point_index: int
    retuned_at: float
    byte_count: int
    iq_i8_b64: str


class WiFiInterfaceInfo(BaseModel):
    name: str
    type: str | None = None
    mac: str | None = None
    channel: int | None = None
    frequency_mhz: int | None = None
    up: bool = False


class WiFiMonitorConfig(BaseModel):
    interface: str = Field(min_length=1, max_length=64)
    channel: int | None = Field(default=None, ge=1, le=196)
    channels: list[int] = Field(default_factory=list)
    bands: list[str] = Field(default_factory=lambda: ["2.4", "5"])
    set_channel: bool = True
    set_monitor: bool = True
    channel_hop_interval_s: float = Field(default=1.0, ge=0.1, le=60.0)
    active_scan: bool = False
    active_scan_interval_s: float = Field(default=60.0, ge=5.0, le=3600.0)
    capture_filter: str = Field(default="", max_length=500)
    command: str = Field(default="scapy", pattern="^(scapy|pyshark|tcpdump|tshark)$")
    max_events: int = Field(default=500, ge=10, le=10000)
    replace_existing: bool = False
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "interface": "wlan0",
                    "bands": ["2.4", "5"],
                    "set_channel": True,
                    "set_monitor": True,
                    "active_scan": False,
                    "command": "tcpdump",
                }
            ]
        }
    }


class WiFiMonitorState(BaseModel):
    wifi_scan_id: str
    status: str
    config: WiFiMonitorConfig
    event_count: int = 0
    returncode: int | None = None
    current_channel: int | None = None


class WiFiMonitorEvent(BaseModel):
    seen_at: float
    interface: str
    raw: str
    kind: str = "wifi"
    ssid: str | None = None
    bssid: str | None = None
    source: str | None = None
    destination: str | None = None
    rssi_dbm: int | None = None
    frequency_mhz: int | None = None
    channel: int | None = None


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


class ApexResource(BaseModel):
    id: str
    kind: str = Field(description="sdr or api")
    label: str
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ApexResourceUpsert(BaseModel):
    id: str = Field(min_length=3, max_length=120)
    kind: str = Field(default="api", description="sdr or api")
    label: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ApexMissionRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=1000)
    resources: list[str] = Field(default_factory=list, description="Resource ids to prioritize")
    constraints: list[str] = Field(default_factory=list)
    execute: bool = False
    max_actions: int = Field(default=6, ge=1, le=20)


class ApexAction(BaseModel):
    tool: str
    target: str
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    rationale: str = ""


class ApexMissionPlan(BaseModel):
    mission_id: str
    objective: str
    resources: list[ApexResource]
    actions: list[ApexAction]
    status: str
    notes: list[str] = Field(default_factory=list)
