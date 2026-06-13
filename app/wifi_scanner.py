from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request

from app.auth import _normalize_token


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_INTERFACE = "wlan0"
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wifi_scanner",
        description="Stream WiFi monitor-mode observations as CSV.",
    )
    parser.add_argument(
        "--backend",
        choices=("scapy", "gateway"),
        default="gateway",
        help="Capture backend. Default: gateway.",
    )
    parser.add_argument("--base-url", default=os.getenv("SDR_GATEWAY_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.getenv("SDR_GATEWAY_API_TOKEN", ""))
    parser.add_argument("--interface", default=os.getenv("SDR_GATEWAY_WIFI_INTERFACE", DEFAULT_INTERFACE))
    parser.add_argument(
        "--band",
        dest="bands",
        action="append",
        choices=("2.4", "5"),
        help="Band to hop. Repeatable. Default: 2.4.",
    )
    parser.add_argument(
        "--channels",
        default="",
        help="Comma-separated channel list. Overrides --band, for example 1,6,11,36,40.",
    )
    parser.add_argument("--channel", type=int, default=None, help="Park on one channel instead of hopping.")
    parser.add_argument("--hop-interval-s", type=float, default=1.0)
    parser.add_argument(
        "--active-scan",
        action="store_true",
        help="Periodically switch to managed mode and run iw scan, then return to monitor mode.",
    )
    parser.add_argument("--active-scan-interval-s", type=float, default=60.0)
    parser.add_argument(
        "--set-monitor",
        dest="set_monitor",
        action="store_true",
        default=False,
        help="Ask the gateway to switch the interface into monitor mode before scanning.",
    )
    parser.add_argument("--no-set-monitor", dest="set_monitor", action="store_false")
    parser.add_argument(
        "--set-channel",
        dest="set_channel",
        action="store_true",
        default=True,
        help="Ask the gateway service to hop/set channels. Requires CAP_NET_ADMIN on the service.",
    )
    parser.add_argument("--no-set-channel", dest="set_channel", action="store_false")
    parser.add_argument(
        "--local-hop",
        dest="local_hop",
        action="store_true",
        default=True,
        help="Hop channels from this CLI process when --set-channel is not used. Default: enabled.",
    )
    parser.add_argument("--no-local-hop", dest="local_hop", action="store_false")
    parser.add_argument("--command", choices=("scapy", "tcpdump", "tshark"), default="scapy")
    parser.add_argument("--capture-filter", default="")
    parser.add_argument("--poll-s", type=float, default=0.25)
    parser.add_argument("--event-limit", type=int, default=500)
    parser.add_argument("--raw-max", type=int, default=180)
    parser.add_argument("--scan-id", default="", help="Attach to an existing gateway WiFi scan.")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Stop existing gateway WiFi scans on this interface before starting.",
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON event per line instead of CSV.")
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--once", action="store_true", help="Print current events once and exit.")
    return parser.parse_args()


def _headers(token: str) -> dict[str, str]:
    headers = {"accept": "application/json"}
    normalized = _normalize_token(token)
    if normalized:
        headers["authorization"] = f"Bearer {normalized}"
        headers["x-api-key"] = normalized
    return headers


def _request_json(
    method: str,
    base_url: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None
    headers = _headers(token)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    req = request.Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=15.0) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else None


def _parse_channels(raw: str) -> list[int]:
    channels: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        channels.append(int(part))
    return list(dict.fromkeys(channels))


def _channels_for_args(args: argparse.Namespace) -> list[int]:
    channels = _parse_channels(args.channels)
    if channels:
        return channels
    if args.channel is not None:
        return [int(args.channel)]
    bands = set(args.bands or ["2.4"])
    channels = []
    if "2.4" in bands:
        channels.extend(WIFI_24_GHZ_HOP_CHANNELS)
    if "5" in bands:
        channels.extend(WIFI_5_GHZ_HOP_CHANNELS)
    return list(dict.fromkeys(channels))


def _start_scan(args: argparse.Namespace) -> str:
    channels = _parse_channels(args.channels)
    payload: dict[str, Any] = {
        "interface": args.interface,
        "set_monitor": bool(args.set_monitor),
        "set_channel": bool(args.set_channel),
        "channel_hop_interval_s": float(args.hop_interval_s),
        "active_scan": bool(args.active_scan),
        "active_scan_interval_s": float(args.active_scan_interval_s),
        "command": args.command,
        "max_events": max(10, int(args.event_limit)),
        "replace_existing": bool(args.replace_existing),
    }
    if args.capture_filter.strip():
        payload["capture_filter"] = args.capture_filter.strip()
    if channels:
        payload["channels"] = channels
    elif args.channel is not None:
        payload["channel"] = int(args.channel)
    else:
        payload["bands"] = args.bands or ["2.4"]

    body = _request_json("POST", args.base_url, "/wifi/scans/start", args.token, payload)
    return str(body["wifi_scan_id"])


def _set_local_channel(interface: str, channel: int) -> bool:
    result = subprocess.run(
        ["iw", "dev", interface, "set", "channel", str(int(channel))],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(f"wifi_scanner: local channel hop failed ch={channel}: {detail}", file=sys.stderr)
        return False
    return True


def _local_hop_loop(args: argparse.Namespace, channels: list[int], stop_event: threading.Event) -> None:
    if len(channels) <= 1:
        return
    index = 0
    _set_local_channel(args.interface, channels[index])
    while not stop_event.wait(max(0.1, float(args.hop_interval_s))):
        index = (index + 1) % len(channels)
        _set_local_channel(args.interface, channels[index])


def _frequency_for_channel(channel: int | None) -> int | None:
    if channel is None:
        return None
    channel = int(channel)
    if 1 <= channel <= 13:
        return 2407 + (channel * 5)
    if channel == 14:
        return 2484
    if 32 <= channel <= 177:
        return 5000 + (channel * 5)
    return None


def _ssid_from_packet(packet: Any) -> str | None:
    try:
        from scapy.layers.dot11 import Dot11Elt
    except ImportError:
        return None
    try:
        elt = packet.getlayer(Dot11Elt)
        while elt is not None:
            if int(getattr(elt, "ID", -1)) == 0:
                raw = bytes(getattr(elt, "info", b"") or b"")
                return raw.decode("utf-8", errors="replace")
            elt = elt.payload.getlayer(Dot11Elt)
    except Exception:
        return None
    return None


def _packet_kind(packet: Any) -> str:
    type_id = int(getattr(packet, "type", -1))
    subtype_id = int(getattr(packet, "subtype", -1))
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
    subtype_name = subtype_names.get((type_id, subtype_id), f"subtype{subtype_id}")
    return f"{type_name}.{subtype_name}"


def _event_from_scapy_packet(
    packet: Any,
    interface: str,
    current_channel: int | None,
) -> dict[str, Any] | None:
    try:
        from scapy.layers.dot11 import Dot11, RadioTap
    except ImportError as exc:
        raise RuntimeError("scapy is required for --backend scapy; install with pip install scapy") from exc
    if not packet.haslayer(Dot11):
        return None
    dot11 = packet.getlayer(Dot11)
    channel = current_channel
    frequency_mhz = _frequency_for_channel(channel)
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
    except Exception:
        pass
    source = getattr(dot11, "addr2", None) or ""
    destination = getattr(dot11, "addr1", None) or ""
    bssid = getattr(dot11, "addr3", None) or ""
    kind = _packet_kind(dot11)
    ssid = _ssid_from_packet(packet)
    raw = _compact_summary(kind, ssid, source, destination, bssid)
    return {
        "seen_at": float(getattr(packet, "time", time.time())),
        "interface": interface,
        "raw": raw,
        "kind": kind,
        "ssid": ssid,
        "bssid": str(bssid or "").lower(),
        "source": str(source or "").lower(),
        "destination": str(destination or "").lower(),
        "rssi_dbm": rssi_dbm,
        "frequency_mhz": frequency_mhz,
        "channel": channel,
    }


def _compact_summary(
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


def _run_scapy(args: argparse.Namespace) -> int:
    try:
        from scapy.all import conf, sniff
    except ImportError:
        print("wifi scan failed: scapy is not installed. Run: pip install scapy", file=sys.stderr)
        return 1

    channels = _channels_for_args(args)
    stop_event = threading.Event()
    channel_lock = threading.Lock()
    current_channel = channels[0] if channels else args.channel
    writer = csv.writer(sys.stdout)
    if not args.json and not args.no_header:
        _print_header(writer)
        sys.stdout.flush()

    def set_current_channel(channel: int) -> bool:
        nonlocal current_channel
        ok = _set_local_channel(args.interface, channel)
        if ok:
            with channel_lock:
                current_channel = int(channel)
        return ok

    def hop_loop() -> None:
        if len(channels) <= 1:
            return
        index = 0
        set_current_channel(channels[index])
        while not stop_event.wait(max(0.1, float(args.hop_interval_s))):
            index = (index + 1) % len(channels)
            set_current_channel(channels[index])

    if args.channel is not None:
        set_current_channel(int(args.channel))
    elif bool(args.local_hop) and len(channels) > 1:
        threading.Thread(target=hop_loop, daemon=True).start()
    elif channels:
        set_current_channel(channels[0])

    counts: dict[tuple[Any, ...], int] = {}

    def on_packet(packet: Any) -> None:
        with channel_lock:
            channel = current_channel
        event = _event_from_scapy_packet(packet, args.interface, channel)
        if event is None:
            return
        count_key = _count_key(event)
        counts[count_key] = counts.get(count_key, 0) + 1
        writer.writerow(_row(event, counts[count_key], int(args.raw_max)))
        sys.stdout.flush()

    def stop_filter(_packet: Any) -> bool:
        return stop_event.is_set() or bool(args.once)

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)

    def handle_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    try:
        conf.sniff_promisc = True
        sniff(
            iface=args.interface,
            prn=on_packet,
            store=False,
            stop_filter=stop_filter,
        )
    finally:
        stop_event.set()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    return 0


def _stop_scan(args: argparse.Namespace, scan_id: str) -> None:
    try:
        _request_json("POST", args.base_url, f"/wifi/scans/{parse.quote(scan_id)}/stop", args.token)
    except Exception:
        pass


def _get_events(args: argparse.Namespace, scan_id: str) -> list[dict[str, Any]]:
    path = f"/wifi/scans/{parse.quote(scan_id)}/events?limit={max(1, min(int(args.event_limit), 1000))}"
    return list(_request_json("GET", args.base_url, path, args.token) or [])


def _format_timestamp(value: Any) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def _event_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("seen_at"),
        event.get("interface"),
        event.get("kind"),
        event.get("source"),
        event.get("destination"),
        event.get("bssid"),
        event.get("ssid"),
        event.get("channel"),
        event.get("raw"),
    )


def _count_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("interface"),
        event.get("kind"),
        event.get("source") or "",
        event.get("destination") or "",
        event.get("bssid") or "",
        event.get("ssid") or "",
        event.get("channel") or "",
    )


def _row(event: dict[str, Any], count: int, raw_max: int) -> list[Any]:
    raw = str(event.get("raw") or "").replace("\r", " ").replace("\n", " ")
    if raw_max > 0 and len(raw) > raw_max:
        raw = f"{raw[:raw_max - 1]}..."
    return [
        _format_timestamp(event.get("seen_at")),
        event.get("interface") or "",
        event.get("source") or "",
        event.get("destination") or "",
        event.get("bssid") or "",
        event.get("channel") or "",
        event.get("frequency_mhz") or "",
        event.get("rssi_dbm") or "",
        event.get("kind") or "",
        event.get("ssid") or "",
        count,
        raw,
    ]


def _print_header(writer: csv.writer) -> None:
    writer.writerow(
        [
            "timestamp",
            "interface",
            "mac_sa",
            "mac_da",
            "bssid",
            "channel",
            "frequency_mhz",
            "rssi_dbm",
            "type",
            "ssid",
            "count",
            "raw",
        ]
    )


def _run(args: argparse.Namespace) -> int:
    own_scan = not bool(args.scan_id)
    channels = _channels_for_args(args)
    stop_event = threading.Event()
    hop_thread: threading.Thread | None = None
    scan_id = args.scan_id or _start_scan(args)
    writer = csv.writer(sys.stdout)
    if not args.no_header:
        _print_header(writer)
        sys.stdout.flush()

    stop_requested = False

    def _handle_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        stop_event.set()

    old_int = signal.signal(signal.SIGINT, _handle_stop)
    old_term = signal.signal(signal.SIGTERM, _handle_stop)
    seen: set[tuple[Any, ...]] = set()
    counts: dict[tuple[Any, ...], int] = {}
    try:
        if args.backend == "scapy" and bool(args.local_hop) and not bool(args.set_channel) and len(channels) > 1:
            hop_thread = threading.Thread(
                target=_local_hop_loop,
                args=(args, channels, stop_event),
                daemon=True,
            )
            hop_thread.start()
        while not stop_requested:
            for event in _get_events(args, scan_id):
                key = _event_key(event)
                if key in seen:
                    continue
                seen.add(key)
                count_key = _count_key(event)
                counts[count_key] = counts.get(count_key, 0) + 1
                if args.json:
                    payload = dict(event)
                    payload["protocol"] = "wifi"
                    payload["count"] = counts[count_key]
                    print(json.dumps(payload, separators=(",", ":")), flush=True)
                else:
                    writer.writerow(_row(event, counts[count_key], int(args.raw_max)))
                    sys.stdout.flush()
            if args.once:
                break
            time.sleep(max(0.05, float(args.poll_s)))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == 422 and "string_pattern_mismatch" in detail and "command" in detail:
            print(
                "wifi scan failed: gateway does not accept this WiFi backend command yet; "
                "restart sdr-gateway so it loads the updated code",
                file=sys.stderr,
            )
            print(detail, file=sys.stderr)
            return 1
        print(f"wifi scan failed: HTTP {exc.code}: {detail or exc.reason}", file=sys.stderr)
        return 1
    except error.URLError as exc:
        print(f"wifi scan failed: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_event.set()
        if hop_thread is not None:
            hop_thread.join(timeout=1.0)
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        if own_scan:
            _stop_scan(args, scan_id)
    return 0


def main() -> int:
    try:
        args = _parse_args()
        if args.backend == "scapy":
            return _run_scapy(args)
        return _run(args)
    except KeyboardInterrupt:
        return 130
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == 422 and "string_pattern_mismatch" in detail and "command" in detail:
            print(
                "wifi scan failed: gateway does not accept this WiFi backend command yet; "
                "restart sdr-gateway so it loads the updated code",
                file=sys.stderr,
            )
            print(detail, file=sys.stderr)
            return 1
        print(f"wifi scan failed: HTTP {exc.code}: {detail or exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"wifi scan failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
