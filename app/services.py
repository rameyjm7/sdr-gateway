from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import io
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from app.models import IQSweepConfig, StreamConfig, SweepConfig, TxBurstConfig, WiFiMonitorConfig
from app.sdr.backend import Device, IQSweepRequest, StreamRequest, SweepRequest, TxBurstRequest

logger = logging.getLogger(__name__)

DEFAULT_STREAM_CHUNK_BYTES = int(os.getenv("SDR_GATEWAY_STREAM_CHUNK_BYTES", str(64 * 1024)))
WIFI_DEFAULT_CAPTURE_FILTER = os.getenv(
    "SDR_GATEWAY_WIFI_CAPTURE_FILTER",
    "type mgt or type data",
)
WIFI_24_GHZ_HOP_CHANNELS = [1, 6, 11]
WIFI_5_GHZ_HOP_CHANNELS = [
    36,
    40,
    44,
    48,
    52,
    56,
    60,
    64,
    100,
    104,
    108,
    112,
    116,
    120,
    124,
    128,
    132,
    136,
    140,
    144,
    149,
    153,
    157,
    161,
    165,
]


class ManagedProcess(Protocol):
    stdout: Any | None
    stderr: Any | None

    def poll(self) -> int | None: ...


class RegistryLike(Protocol):
    def list_devices(self) -> list[Device]: ...

    def backend_for_device(self, device_id: str) -> Any: ...


@dataclass
class WiFiMonitorSession:
    id: str
    config: WiFiMonitorConfig
    process: ManagedProcess | None
    status: str = "running"
    events: deque[dict[str, Any]] | None = None
    event_count: int = 0
    returncode: int | None = None
    channels: list[int] = field(default_factory=list)
    current_channel: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    control_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stop: threading.Event | None = None


class WiFiMonitorManager:
    def __init__(self) -> None:
        self._sessions: dict[str, WiFiMonitorSession] = {}

    def list_interfaces(self) -> list[dict[str, Any]]:
        interfaces = self._iw_interfaces()
        if interfaces:
            return sorted(interfaces, key=lambda item: str(item.get("name") or ""))
        by_name = {str(item.get("name")): item for item in interfaces if item.get("name")}
        for name in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
            by_name.setdefault(
                name,
                {
                    "name": name,
                    "type": None,
                    "mac": self._read_sys_text(f"/sys/class/net/{name}/address"),
                    "channel": None,
                    "frequency_mhz": None,
                    "up": self._interface_up(name),
                },
            )
        return sorted(by_name.values(), key=lambda item: str(item.get("name") or ""))

    def list_states(self) -> list[WiFiMonitorSession]:
        self._refresh()
        return list(self._sessions.values())

    def get(self, wifi_scan_id: str) -> WiFiMonitorSession:
        self._refresh()
        return self._sessions[wifi_scan_id]

    def start(self, config: WiFiMonitorConfig) -> WiFiMonitorSession:
        self._validate_interface(config)
        if config.command == "scapy" and config.active_scan:
            raise ValueError("active_scan with command=scapy is not supported yet; use tcpdump or tshark")
        existing_ids = [
            wifi_scan_id
            for wifi_scan_id, session in list(self._sessions.items())
            if session.config.interface == config.interface
        ]
        if existing_ids and config.replace_existing:
            for wifi_scan_id in existing_ids:
                self.stop(wifi_scan_id)
        elif existing_ids:
            raise ValueError(f"WiFi interface {config.interface} is already in use")
        channels = self._channels_for_config(config)
        if config.set_monitor:
            self._set_interface_type(config.interface, "monitor")
        if channels and config.set_channel:
            self._set_channel(config.interface, channels[0])

        session = WiFiMonitorSession(
            id=str(uuid.uuid4()),
            config=config,
            process=None,
            events=deque(maxlen=int(config.max_events)),
            channels=channels,
            current_channel=channels[0] if channels else config.channel,
            _stop=threading.Event(),
        )
        self._sessions[session.id] = session
        if config.command == "scapy":
            threading.Thread(target=self._collect_scapy_events, args=(session,), daemon=True).start()
        else:
            session.process = self._start_capture(config)
            threading.Thread(target=self._collect_events, args=(session,), daemon=True).start()
        if len(channels) > 1 and config.set_channel:
            threading.Thread(target=self._hop_channels, args=(session,), daemon=True).start()
        if config.active_scan:
            threading.Thread(target=self._active_scan_loop, args=(session,), daemon=True).start()
        return session

    def stop(self, wifi_scan_id: str) -> None:
        session = self._sessions.pop(wifi_scan_id)
        if session._stop is not None:
            session._stop.set()
        with session.lock:
            if session.process is not None:
                self._terminate(session.process)
                session.returncode = session.process.poll()
                session.process = None
        session.status = "stopped"

    def stop_all(self) -> None:
        for wifi_scan_id in list(self._sessions.keys()):
            try:
                self.stop(wifi_scan_id)
            except Exception:
                continue

    def recent_events(self, wifi_scan_id: str, limit: int = 100) -> list[dict[str, Any]]:
        session = self.get(wifi_scan_id)
        events = list(session.events or [])
        return events[-max(1, min(int(limit), 1000)) :]

    def _refresh(self) -> None:
        for session in self._sessions.values():
            if session.status == "running":
                with session.lock:
                    process = session.process
                if process is None:
                    continue
                rc = process.poll()
                if rc is not None and session._stop is not None and not session._stop.is_set():
                    try:
                        with session.lock:
                            session.process = self._start_capture(session.config)
                    except Exception:
                        session.status = "failed"
                        session.returncode = int(rc)

    def _collect_events(self, session: WiFiMonitorSession) -> None:
        if session.events is None or session._stop is None:
            return
        while not session._stop.is_set():
            with session.lock:
                process = session.process
                stdout = process.stdout if process is not None else None
            if stdout is None:
                time.sleep(0.05)
                continue
            line = stdout.readline()
            if not line:
                if process is not None and process.poll() is not None:
                    try:
                        with session.lock:
                            if session.process is process:
                                session.process = self._start_capture(session.config)
                    except Exception:
                        session.status = "failed"
                        session.returncode = process.poll()
                        break
                time.sleep(0.02)
                continue
            parsed = self._parse_wifi_line(line.rstrip(), session.config.interface)
            if parsed:
                session.events.append(parsed)
                session.event_count += 1
        self._refresh()

    def _collect_scapy_events(self, session: WiFiMonitorSession) -> None:
        if session.events is None or session._stop is None:
            return
        try:
            from scapy.all import conf, sniff
            from scapy.layers.dot11 import Dot11
        except ImportError:
            session.status = "failed"
            self._append_event(
                session,
                {
                    "seen_at": time.time(),
                    "interface": session.config.interface,
                    "raw": "scapy is required for WiFi command=scapy",
                    "kind": "control_error",
                },
            )
            return

        def on_packet(packet: Any) -> None:
            if session._stop is not None and session._stop.is_set():
                return
            if not packet.haslayer(Dot11):
                return
            event = self._event_from_scapy_packet(packet, session)
            if event:
                self._append_event(session, event)

        conf.sniff_promisc = True
        while session._stop is not None and not session._stop.is_set():
            try:
                sniff(
                    iface=session.config.interface,
                    prn=on_packet,
                    store=False,
                    timeout=1.0,
                )
            except Exception as exc:
                session.status = "failed"
                self._append_event(
                    session,
                    {
                        "seen_at": time.time(),
                        "interface": session.config.interface,
                        "raw": f"scapy sniff failed error={exc}",
                        "kind": "control_error",
                    },
                )
                time.sleep(1.0)
        session.status = "stopped"

    def _hop_channels(self, session: WiFiMonitorSession) -> None:
        assert session._stop is not None
        index = 0
        while not session._stop.is_set():
            time.sleep(float(session.config.channel_hop_interval_s))
            if session._stop.is_set() or not session.channels:
                break
            index = (index + 1) % len(session.channels)
            channel = session.channels[index]
            try:
                with session.control_lock:
                    self._set_channel(session.config.interface, channel)
                session.current_channel = channel
            except Exception as exc:
                self._append_event(
                    session,
                    {
                        "seen_at": time.time(),
                        "interface": session.config.interface,
                        "raw": f"channel hop failed channel={channel} error={exc}",
                        "kind": "control_error",
                        "channel": channel,
                    },
                )

    def _active_scan_loop(self, session: WiFiMonitorSession) -> None:
        assert session._stop is not None
        while not session._stop.wait(float(session.config.active_scan_interval_s)):
            self._run_active_scan(session)

    def _run_active_scan(self, session: WiFiMonitorSession) -> None:
        interface = session.config.interface
        with session.control_lock:
            with session.lock:
                old_process = session.process
                session.process = None
                if old_process is not None:
                    self._terminate(old_process)
            try:
                self._set_interface_type(interface, "managed")
                channels = session.channels or self._channels_for_config(session.config)
                command = ["iw", "dev", interface, "scan"]
                frequencies = [str(freq) for ch in channels if (freq := self._frequency_for_channel(ch))]
                if frequencies:
                    command.extend(["freq", *frequencies])
                result = subprocess.run(command, text=True, capture_output=True, timeout=45, check=False)
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or result.stdout).strip())
                for event in self._parse_iw_scan_output(result.stdout, interface):
                    self._append_event(session, event)
            except Exception as exc:
                self._append_event(
                    session,
                    {
                        "seen_at": time.time(),
                        "interface": interface,
                        "raw": f"active scan failed error={exc}",
                        "kind": "control_error",
                    },
                )
            finally:
                with session.lock:
                    if session._stop is not None and session._stop.is_set():
                        return
                with contextlib.suppress(Exception):
                    self._set_interface_type(interface, "monitor")
                    if session.current_channel is not None:
                        self._set_channel(interface, session.current_channel)
                with session.lock:
                    if session._stop is None or session._stop.is_set():
                        return
                    session.process = self._start_capture(session.config)

    def _append_event(self, session: WiFiMonitorSession, event: dict[str, Any]) -> None:
        if session.events is None:
            return
        session.events.append(event)
        session.event_count += 1

    @staticmethod
    def _start_capture(config: WiFiMonitorConfig) -> subprocess.Popen[str]:
        return subprocess.Popen(
            WiFiMonitorManager._capture_command(config),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    @staticmethod
    def _capture_command(config: WiFiMonitorConfig) -> list[str]:
        capture_filter = config.capture_filter.strip() or WIFI_DEFAULT_CAPTURE_FILTER
        if config.command == "tshark":
            command = ["tshark", "-i", config.interface, "-l", "-n"]
            if capture_filter:
                command.extend(["-f", capture_filter])
            return command
        command = ["tcpdump", "-i", config.interface, "-I", "-l", "-e", "-n", "-s", "256"]
        if capture_filter:
            command.extend(capture_filter.split())
        return command

    @staticmethod
    def _channels_for_config(config: WiFiMonitorConfig) -> list[int]:
        if config.channels:
            return list(dict.fromkeys(int(ch) for ch in config.channels))
        if config.channel is not None:
            return [int(config.channel)]
        bands = {str(band).strip().lower() for band in config.bands}
        channels: list[int] = []
        if {"2.4", "2g", "2ghz", "2.4ghz"} & bands:
            channels.extend(WIFI_24_GHZ_HOP_CHANNELS)
        if {"5", "5g", "5ghz"} & bands:
            channels.extend(WIFI_5_GHZ_HOP_CHANNELS)
        return channels or list(WIFI_24_GHZ_HOP_CHANNELS)

    @staticmethod
    def _set_interface_type(interface: str, mode: str) -> None:
        WiFiMonitorManager._run_checked(["ip", "link", "set", interface, "down"])
        WiFiMonitorManager._run_checked(["iw", "dev", interface, "set", "type", mode])
        WiFiMonitorManager._run_checked(["ip", "link", "set", interface, "up"])

    @staticmethod
    def _set_channel(interface: str, channel: int) -> None:
        WiFiMonitorManager._run_checked(["iw", "dev", interface, "set", "channel", str(int(channel))])

    @staticmethod
    def _frequency_for_channel(channel: int) -> int | None:
        channel = int(channel)
        if 1 <= channel <= 13:
            return 2407 + (channel * 5)
        if channel == 14:
            return 2484
        if 32 <= channel <= 177:
            return 5000 + (channel * 5)
        return None

    @staticmethod
    def _parse_iw_scan_output(output: str, interface: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw in output.splitlines():
            line = raw.strip()
            if line.startswith("BSS "):
                if current:
                    events.append(current)
                bssid = line.split()[1].split("(")[0].lower()
                current = {
                    "seen_at": time.time(),
                    "interface": interface,
                    "raw": "",
                    "kind": "active_ap",
                    "ssid": None,
                    "bssid": bssid,
                    "source": bssid,
                    "destination": None,
                    "rssi_dbm": None,
                    "frequency_mhz": None,
                    "channel": None,
                }
                continue
            if current is None:
                continue
            current["raw"] = f"{current.get('raw')}\n{line}".strip()
            if line.startswith("freq:"):
                with contextlib.suppress(ValueError):
                    freq = int(line.split(":", 1)[1].strip())
                    current["frequency_mhz"] = freq
                    current["channel"] = WiFiMonitorManager._wifi_channel_from_frequency(freq)
            elif line.startswith("signal:"):
                match = re.search(r"(-?\d+(?:\.\d+)?)", line)
                if match:
                    current["rssi_dbm"] = int(float(match.group(1)))
            elif line.startswith("SSID:"):
                current["ssid"] = line.split(":", 1)[1].strip()
        if current:
            events.append(current)
        return events

    @staticmethod
    def _parse_wifi_line(line: str, interface: str) -> dict[str, Any] | None:
        text = line.strip()
        if not text:
            return None
        lower = text.lower()
        kind = "wifi"
        if "beacon" in lower:
            kind = "beacon"
        elif "probe request" in lower or "probe-req" in lower:
            kind = "probe_request"
        elif "probe response" in lower or "probe-resp" in lower:
            kind = "probe_response"
        elif "data" in lower:
            kind = "data"

        ssid = None
        for pattern in (r"Beacon \(([^)]*)\)", r"Probe Request \(([^)]*)\)", r"Probe Response \(([^)]*)\)"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                ssid = match.group(1)
                break
        bssid = WiFiMonitorManager._match_mac(text, "BSSID")
        source = WiFiMonitorManager._match_mac(text, "SA") or WiFiMonitorManager._match_mac(text, "TA")
        destination = WiFiMonitorManager._match_mac(text, "DA") or WiFiMonitorManager._match_mac(text, "RA")
        rssi_match = re.search(r"(-\d+)\s*dBm", text, flags=re.IGNORECASE)
        freq_match = re.search(r"\b(24\d{2}|5\d{3}|6\d{3})\s*MHz\b", text, flags=re.IGNORECASE)
        frequency_mhz = int(freq_match.group(1)) if freq_match else None
        return {
            "seen_at": time.time(),
            "interface": interface,
            "raw": text,
            "kind": kind,
            "ssid": ssid,
            "bssid": bssid,
            "source": source,
            "destination": destination,
            "rssi_dbm": int(rssi_match.group(1)) if rssi_match else None,
            "frequency_mhz": frequency_mhz,
            "channel": WiFiMonitorManager._wifi_channel_from_frequency(frequency_mhz),
        }

    @staticmethod
    def _event_from_scapy_packet(packet: Any, session: WiFiMonitorSession) -> dict[str, Any] | None:
        try:
            from scapy.layers.dot11 import Dot11, RadioTap
        except ImportError:
            return None
        if not packet.haslayer(Dot11):
            return None
        dot11 = packet.getlayer(Dot11)
        channel = session.current_channel
        frequency_mhz = WiFiMonitorManager._frequency_for_channel(channel)
        rssi_dbm = None
        try:
            if packet.haslayer(RadioTap):
                radio = packet.getlayer(RadioTap)
                signal = getattr(radio, "dBm_AntSignal", None)
                if signal is not None:
                    rssi_dbm = int(signal)
                channel_freq = getattr(radio, "ChannelFrequency", None)
                if channel_freq:
                    frequency_mhz = int(channel_freq)
                    inferred_channel = WiFiMonitorManager._wifi_channel_from_frequency(frequency_mhz)
                    if inferred_channel is not None:
                        channel = inferred_channel
        except Exception:
            pass
        source = str(getattr(dot11, "addr2", None) or "").lower()
        destination = str(getattr(dot11, "addr1", None) or "").lower()
        bssid = str(getattr(dot11, "addr3", None) or "").lower()
        kind = WiFiMonitorManager._scapy_packet_kind(dot11)
        ssid = WiFiMonitorManager._scapy_ssid(packet)
        raw = WiFiMonitorManager._wifi_compact_summary(
            kind=kind,
            ssid=ssid,
            source=source,
            destination=destination,
            bssid=bssid,
        )
        return {
            "seen_at": float(getattr(packet, "time", time.time())),
            "interface": session.config.interface,
            "raw": raw,
            "kind": kind,
            "ssid": ssid,
            "bssid": bssid,
            "source": source,
            "destination": destination,
            "rssi_dbm": rssi_dbm,
            "frequency_mhz": frequency_mhz,
            "channel": channel,
        }

    @staticmethod
    def _wifi_compact_summary(
        *,
        kind: str,
        ssid: str | None,
        source: str,
        destination: str,
        bssid: str,
    ) -> str:
        parts = [kind]
        if ssid:
            parts.append(f"ssid={ssid}")
        if source:
            parts.append(f"sa={source}")
        if destination:
            parts.append(f"da={destination}")
        if bssid:
            parts.append(f"bssid={bssid}")
        return " ".join(parts)

    @staticmethod
    def _scapy_packet_kind(dot11: Any) -> str:
        type_id = int(getattr(dot11, "type", -1))
        subtype_id = int(getattr(dot11, "subtype", -1))
        type_name = {0: "mgmt", 1: "ctrl", 2: "data", 3: "ext"}.get(type_id, f"type{type_id}")
        subtype_names = {
            (0, 0): "association_request",
            (0, 1): "association_response",
            (0, 2): "reassociation_request",
            (0, 3): "reassociation_response",
            (0, 4): "probe_request",
            (0, 5): "probe_response",
            (0, 8): "beacon",
            (0, 9): "atim",
            (0, 10): "disassociation",
            (0, 11): "authentication",
            (0, 12): "deauthentication",
            (0, 13): "action",
            (1, 8): "block_ack_request",
            (1, 9): "block_ack",
            (1, 10): "ps_poll",
            (1, 11): "rts",
            (1, 12): "cts",
            (1, 13): "ack",
            (1, 14): "cf_end",
            (1, 15): "cf_end_ack",
            (2, 0): "data",
            (2, 4): "null",
            (2, 8): "qos_data",
            (2, 12): "qos_null",
        }
        return f"{type_name}.{subtype_names.get((type_id, subtype_id), f'subtype{subtype_id}')}"

    @staticmethod
    def _scapy_ssid(packet: Any) -> str | None:
        try:
            from scapy.layers.dot11 import Dot11Elt
        except ImportError:
            return None
        try:
            elt = packet.getlayer(Dot11Elt)
            while elt is not None:
                if int(getattr(elt, "ID", -1)) == 0:
                    raw = bytes(getattr(elt, "info", b"") or b"")
                    if not raw or not raw.replace(b"\x00", b"").strip():
                        return None
                    return raw.decode("utf-8", errors="replace")
                elt = elt.payload.getlayer(Dot11Elt)
        except Exception:
            return None
        return None

    @staticmethod
    def _match_mac(text: str, label: str) -> str | None:
        match = re.search(rf"\b{re.escape(label)}:(([0-9a-fA-F]{{2}}:){{5}}[0-9a-fA-F]{{2}})", text)
        return match.group(1).lower() if match else None

    @staticmethod
    def _wifi_channel_from_frequency(frequency_mhz: int | None) -> int | None:
        if frequency_mhz is None:
            return None
        if frequency_mhz == 2484:
            return 14
        if 2412 <= frequency_mhz <= 2472:
            return ((frequency_mhz - 2412) // 5) + 1
        if 5000 <= frequency_mhz <= 5900:
            return (frequency_mhz - 5000) // 5
        if 5955 <= frequency_mhz <= 7115:
            return ((frequency_mhz - 5955) // 5) + 1
        return None

    @staticmethod
    def _iw_interfaces() -> list[dict[str, Any]]:
        if shutil.which("iw") is None:
            return []
        try:
            result = subprocess.run(["iw", "dev"], text=True, capture_output=True, timeout=4, check=False)
        except Exception:
            return []
        interfaces: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line.startswith("Interface "):
                if current:
                    current["up"] = WiFiMonitorManager._interface_up(str(current.get("name") or ""))
                    interfaces.append(current)
                current = {
                    "name": line.split(maxsplit=1)[1],
                    "type": None,
                    "mac": None,
                    "channel": None,
                    "frequency_mhz": None,
                    "up": False,
                }
                continue
            if current is None:
                continue
            if line.startswith("addr "):
                current["mac"] = line.split(maxsplit=1)[1].lower()
            elif line.startswith("type "):
                current["type"] = line.split(maxsplit=1)[1]
            elif line.startswith("channel "):
                parts = line.split()
                try:
                    current["channel"] = int(parts[1])
                except (IndexError, ValueError):
                    pass
                match = re.search(r"\((\d+)\s*MHz\)", line)
                if match:
                    current["frequency_mhz"] = int(match.group(1))
        if current:
            current["up"] = WiFiMonitorManager._interface_up(str(current.get("name") or ""))
            interfaces.append(current)
        return interfaces

    @staticmethod
    def _read_sys_text(path: str) -> str | None:
        try:
            return open(path, encoding="utf-8").read().strip()
        except OSError:
            return None

    @staticmethod
    def _interface_up(name: str) -> bool:
        state = WiFiMonitorManager._read_sys_text(f"/sys/class/net/{name}/operstate")
        return state in {"up", "unknown"}

    def _validate_interface(self, config: WiFiMonitorConfig) -> None:
        interface = config.interface
        if "/" in interface or interface in {"", ".", ".."}:
            raise ValueError("invalid interface name")
        if not os.path.exists(f"/sys/class/net/{interface}"):
            raise KeyError(f"Unknown WiFi interface '{interface}'")
        if config.command == "scapy":
            try:
                import scapy.all  # noqa: F401
            except ImportError as exc:
                raise ValueError("scapy is required for WiFi command=scapy") from exc
            return
        if shutil.which(config.command) is None:
            raise ValueError(f"{config.command} is required for WiFi monitor scans")

    @staticmethod
    def _run_checked(command: list[str]) -> None:
        result = subprocess.run(command, text=True, capture_output=True, timeout=8, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(command)} failed: {(result.stderr or result.stdout).strip()}")

    @staticmethod
    def _terminate(process: ManagedProcess) -> None:
        try:
            if hasattr(process, "pid"):
                os.killpg(process.pid, 15)
            else:
                process.terminate()  # type: ignore[attr-defined]
        except Exception:
            pass
        deadline = time.time() + 2.0
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            try:
                if hasattr(process, "pid"):
                    os.killpg(process.pid, 9)
            except Exception:
                pass


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
