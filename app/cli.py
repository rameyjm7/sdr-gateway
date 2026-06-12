from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib import error, request

import uvicorn

from app.auth import _normalize_token
from app.sdr.registry import BackendRegistry


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_TEST_CENTER_FREQ_HZ = 102_100_000
DEFAULT_TEST_LNA_GAIN_DB = 16
DEFAULT_TEST_VGA_GAIN_DB = 20
DEFAULT_TEST_CAPTURE_COUNT = 2
DEFAULT_TEST_CHUNK_SIZE = 16_384


@dataclass(frozen=True)
class ProbeResult:
    id: str
    driver: str
    label: str
    busy: str
    owner: str
    serial: str
    notes: str
    freq_min_hz: int
    freq_max_hz: int
    max_sample_rate_sps: int


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sdr-gateway",
        description="SDR Gateway service and device probe CLI.",
    )
    parser.add_argument("-p", "--probe", action="store_true", help="Probe SDR devices and print busy status.")
    parser.add_argument(
        "--driver",
        default="",
        help="Filter probe/test results to one driver or device prefix, for example antsdre200 or antsdre200:0.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run the stream FFT/power probe against the matching device(s).",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SDR_GATEWAY_BASE_URL", DEFAULT_BASE_URL),
        help=f"Gateway base URL for probe mode. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("SDR_GATEWAY_API_TOKEN", ""),
        help="Bearer token or X-API-Key for probe mode. Defaults to SDR_GATEWAY_API_TOKEN.",
    )
    parser.add_argument(
        "--format",
        choices=("entries", "table"),
        default="entries",
        help="Probe output format. Default: entries",
    )
    parser.add_argument("--host", default=os.getenv("SDR_GATEWAY_HOST", "127.0.0.1"), help="Bind host.")
    parser.add_argument("--port", type=int, default=int(os.getenv("SDR_GATEWAY_PORT", "8080")), help="Bind port.")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload while developing locally.",
    )
    return parser.parse_args()


def _http_headers(token: str) -> dict[str, str]:
    headers = {"accept": "application/json"}
    normalized_token = _normalize_token(token)
    if normalized_token:
        headers["authorization"] = f"Bearer {normalized_token}"
        headers["x-api-key"] = normalized_token
    return headers


def _probe_via_http(base_url: str, token: str) -> tuple[list[ProbeResult], str | None]:
    url = f"{base_url.rstrip('/')}/devices"
    headers = _http_headers(token)
    req = request.Request(url, headers=headers, method="GET")
    with request.urlopen(req, timeout=5.0) as response:
        payload = json.loads(response.read().decode("utf-8"))

    results: list[ProbeResult] = []
    for item in payload:
        occupied = bool(item.get("occupied", False))
        owner = ""
        if occupied:
            owner_kind = str(item.get("occupied_by", "") or "session")
            owner_id = str(item.get("occupied_id", "") or "").strip()
            owner = f"{owner_kind}:{owner_id}" if owner_id else owner_kind
        results.append(
            ProbeResult(
                id=str(item.get("id", "")),
                driver=str(item.get("driver", "")),
                label=str(item.get("label", "")),
                busy="yes" if occupied else "no",
                owner=owner,
                serial=str(item.get("serial", "") or ""),
                notes=str(item.get("notes", "") or ""),
                freq_min_hz=int(item.get("freq_min_hz", 1_000_000)),
                freq_max_hz=int(item.get("freq_max_hz", 6_000_000_000)),
                max_sample_rate_sps=int(item.get("max_sample_rate_sps", 20_000_000)),
            )
        )
    return results, None


def _probe_local_registry() -> tuple[list[ProbeResult], str]:
    registry = BackendRegistry()
    results = [
        ProbeResult(
            id=device.id,
            driver=device.driver,
            label=device.label,
            busy="unknown",
            owner="",
            serial=device.serial or "",
            notes=device.notes or "",
            freq_min_hz=int(device.freq_min_hz),
            freq_max_hz=int(device.freq_max_hz),
            max_sample_rate_sps=int(device.max_sample_rate_sps),
        )
        for device in registry.list_devices()
    ]
    return results, "gateway API unreachable; busy status unavailable without a running server"


def _filter_rows(rows: list[ProbeResult], selector: str) -> list[ProbeResult]:
    selector = selector.strip().lower()
    if not selector:
        return rows
    return [
        row for row in rows
        if row.driver.lower() == selector
        or row.id.lower() == selector
        or row.id.lower().startswith(f"{selector}:")
    ]


def _format_table(rows: list[ProbeResult]) -> str:
    headers = ("ID", "Driver", "Label", "Busy", "Owner", "Serial", "Notes")
    body = [
        (
            row.id,
            row.driver,
            row.label,
            row.busy,
            row.owner,
            row.serial,
            row.notes,
        )
        for row in rows
    ]
    widths = [
        max(len(header), *(len(values[index]) for values in body)) if body else len(header)
        for index, header in enumerate(headers)
    ]
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for values in body:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(values)))
    return "\n".join(lines)


def _format_entries(rows: list[ProbeResult]) -> str:
    blocks: list[str] = []
    for index, row in enumerate(rows, start=1):
        blocks.append(
            "\n".join(
                [
                    f"[{index}] {row.id}",
                    f"  driver: {row.driver}",
                    f"  label:  {row.label}",
                    f"  busy:   {row.busy}",
                    f"  owner:  {row.owner or '-'}",
                    f"  serial: {row.serial or '-'}",
                    f"  notes:  {row.notes or '-'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def _print_probe_rows(rows: list[ProbeResult], note: str | None, output_format: str) -> None:
    if not rows:
        print("No SDR devices found.")
        if note:
            print(f"note: {note}")
        return

    if output_format == "table":
        print(_format_table(rows))
    else:
        print(_format_entries(rows))
    if note:
        print(f"\nnote: {note}")


def _probe_test_payload(row: ProbeResult) -> dict[str, Any]:
    sample_rate = max(200_000, min(int(row.max_sample_rate_sps or 20_000_000), 20_000_000))
    center_freq = min(max(DEFAULT_TEST_CENTER_FREQ_HZ, int(row.freq_min_hz)), int(row.freq_max_hz))
    return {
        "device_id": row.id,
        "center_freq_hz": center_freq,
        "sample_rate_sps": sample_rate,
        "lna_gain_db": DEFAULT_TEST_LNA_GAIN_DB,
        "vga_gain_db": DEFAULT_TEST_VGA_GAIN_DB,
        "amp_enable": False,
        "capture_count": DEFAULT_TEST_CAPTURE_COUNT,
        "chunk_size": DEFAULT_TEST_CHUNK_SIZE,
    }


def _run_stream_test(base_url: str, token: str, row: ProbeResult) -> tuple[bool, str]:
    url = f"{base_url.rstrip('/')}/streams/probe"
    payload = json.dumps(_probe_test_payload(row)).encode("utf-8")
    headers = _http_headers(token)
    headers["content-type"] = "application/json"
    req = request.Request(url, data=payload, headers=headers, method="POST")
    with request.urlopen(req, timeout=20.0) as response:
        result = json.loads(response.read().decode("utf-8"))

    alive = bool(result.get("alive"))
    comparison = result.get("comparison", {}) or {}
    captures = result.get("captures", []) or []
    first = captures[0] if len(captures) > 0 else {}
    second = captures[1] if len(captures) > 1 else {}
    summary = (
        f"{row.id}: alive={alive} "
        f"mean1={float(first.get('mean_power_db', -255.0)):.2f}dB "
        f"peak1={float(first.get('peak_power_db', -255.0)):.2f}dB "
        f"mean2={float(second.get('mean_power_db', -255.0)):.2f}dB "
        f"peak2={float(second.get('peak_power_db', -255.0)):.2f}dB "
        f"same_bytes={bool(comparison.get('same_bytes', False))} "
        f"fft_delta={float(comparison.get('mean_abs_fft_delta_db', 0.0)):.6f}dB"
    )
    return alive, summary


def _run_probe(args: argparse.Namespace) -> int:
    try:
        rows, note = _probe_via_http(args.base_url, args.token)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        print(f"probe failed: {exc.code} {exc.reason}", file=sys.stderr)
        if exc.code == 401:
            print(
                "hint: set SDR_GATEWAY_API_TOKEN or pass --token so probe can authenticate",
                file=sys.stderr,
            )
        if detail:
            print(detail, file=sys.stderr)
        return 1
    except (error.URLError, TimeoutError):
        rows, note = _probe_local_registry()
    except Exception as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1

    rows = _filter_rows(rows, args.driver)
    if not rows:
        print("No matching SDR devices found.")
        return 1

    _print_probe_rows(rows, note, args.format)

    if not args.test:
        return 0

    if note:
        print("\nstream test requires a running gateway API.", file=sys.stderr)
        return 1

    failures = 0
    for row in rows:
        try:
            alive, summary = _run_stream_test(args.base_url, args.token, row)
            print(f"\nTEST {summary}")
            if not alive:
                failures += 1
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            print(f"\nTEST {row.id}: failed: {exc.code} {exc.reason}", file=sys.stderr)
            if detail:
                print(detail, file=sys.stderr)
            failures += 1
        except Exception as exc:
            print(f"\nTEST {row.id}: failed: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


def _run_server(args: argparse.Namespace) -> int:
    ws_ping_interval = os.getenv("SDR_GATEWAY_WS_PING_INTERVAL", "0").strip()
    ws_ping_interval_value = None if ws_ping_interval in {"", "0", "none", "None"} else float(ws_ping_interval)
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        ws_ping_interval=ws_ping_interval_value,
    )
    return 0


def main() -> int:
    args = _parse_args()
    if args.probe or args.test or args.driver:
        return _run_probe(args)
    return _run_server(args)


if __name__ == "__main__":
    raise SystemExit(main())
