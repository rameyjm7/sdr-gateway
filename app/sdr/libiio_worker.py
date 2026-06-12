from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

import numpy as np

try:
    import adi  # type: ignore
except Exception as exc:  # pragma: no cover - runtime dependency
    print(f"pyadi-iio import failed: {exc}", file=sys.stderr)
    print(f"PYTHONPATH={os.getenv('PYTHONPATH', '')}", file=sys.stderr)
    raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="libiio IQ worker for AD9361-class radios.")
    parser.add_argument("--mode", choices=("rx", "tx"), default="rx")
    parser.add_argument("--uri", required=True)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--center-freq-hz", type=int, required=True)
    parser.add_argument("--sample-rate-sps", type=int, required=True)
    parser.add_argument("--baseband-filter-hz", type=int, default=0)
    parser.add_argument("--lna-gain-db", type=int, default=0)
    parser.add_argument("--vga-gain-db", type=int, default=0)
    parser.add_argument("--duration-seconds", type=int, default=0)
    parser.add_argument("--num-samples", type=int, default=0)
    parser.add_argument("--rx-buffer-size", type=int, default=16384)
    parser.add_argument("--warmup-reads", type=int, default=4)
    parser.add_argument("--tx-gain-db", type=int, default=20)
    parser.add_argument("--iq-file", type=str, default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    return parser.parse_args()


def _load_radio(uri: str):
    # pyadi-iio exposes the AD936x family as a single convenience class.
    return adi.ad9361(uri=uri)


def _configure_common_rx(sdr, args: argparse.Namespace) -> None:
    sdr.rx_enabled_channels = [0]
    try:
        sdr.rx_buffer_size = int(max(1024, args.rx_buffer_size))
    except Exception:
        pass
    sdr.sample_rate = float(args.sample_rate_sps)
    sdr.rx_lo = float(args.center_freq_hz)
    if args.baseband_filter_hz and args.baseband_filter_hz > 0:
        sdr.rx_rf_bandwidth = float(args.baseband_filter_hz)

    try:
        sdr.gain_control_mode_chan0 = "manual"
    except Exception:
        try:
            sdr.gain_control_mode = "manual"
        except Exception:
            pass

    try:
        sdr.rx_hardwaregain_chan0 = float(max(0, args.lna_gain_db + args.vga_gain_db))
    except Exception:
        try:
            sdr.rx_hardwaregain = float(max(0, args.lna_gain_db + args.vga_gain_db))
        except Exception:
            pass


def _configure_common_tx(sdr, args: argparse.Namespace) -> None:
    sdr.tx_enabled_channels = [0]
    sdr.sample_rate = float(args.sample_rate_sps)
    sdr.tx_lo = float(args.center_freq_hz)
    if args.baseband_filter_hz and args.baseband_filter_hz > 0:
        sdr.tx_rf_bandwidth = float(args.baseband_filter_hz)

    try:
        sdr.tx_hardwaregain_chan0 = float(args.tx_gain_db)
    except Exception:
        try:
            sdr.tx_hardwaregain = float(args.tx_gain_db)
        except Exception:
            pass


def _as_int8_iq(raw: bytes) -> np.ndarray:
    samples = np.frombuffer(raw, dtype=np.int8)
    if samples.size < 2:
        return np.zeros(0, dtype=np.complex64)
    samples = samples[: samples.size - (samples.size % 2)]
    i = samples[0::2].astype(np.float32)
    q = samples[1::2].astype(np.float32)
    return (i + 1j * q) / 127.0


def _to_int8_iq_bytes(samples) -> bytes:
    arr = np.asarray(samples)
    if arr.size == 0:
        return b""
    scale = 127.0 if arr.dtype.kind in {"f", "c"} else 64.0
    arr = np.asarray(arr, dtype=np.complex64).reshape(-1)
    i = np.clip(np.rint(arr.real * scale), -128, 127).astype(np.int8, copy=False)
    q = np.clip(np.rint(arr.imag * scale), -128, 127).astype(np.int8, copy=False)
    out = np.empty(i.size * 2, dtype=np.int8)
    out[0::2] = i
    out[1::2] = q
    return out.tobytes()


def _run_rx(sdr, args: argparse.Namespace) -> int:
    _configure_common_rx(sdr, args)

    warmup_reads = max(0, int(args.warmup_reads))
    for _ in range(warmup_reads):
        try:
            sdr.rx()
        except Exception:
            break

    start_ts = time.time()
    max_samples = int(args.num_samples) if args.num_samples > 0 else 0
    duration_s = int(args.duration_seconds) if args.duration_seconds > 0 else 0
    produced_samples = 0
    out = sys.stdout.buffer

    try:
        while True:
            if duration_s and (time.time() - start_ts) >= duration_s:
                break
            if max_samples and produced_samples >= max_samples:
                break

            try:
                samples = sdr.rx()
            except Exception:
                continue

            if isinstance(samples, list):
                if not samples:
                    continue
                samples = samples[0]

            chunk = np.asarray(samples, dtype=np.complex64).reshape(-1)
            if chunk.size == 0:
                continue
            out.write(_to_int8_iq_bytes(chunk))
            out.flush()
            produced_samples += int(chunk.size)
    finally:
        try:
            if hasattr(sdr, "rx_destroy_buffer"):
                sdr.rx_destroy_buffer()
        except Exception:
            pass
    return 0


def _run_tx(sdr, args: argparse.Namespace) -> int:
    _configure_common_tx(sdr, args)

    iq_path = Path(args.iq_file)
    raw = iq_path.read_bytes()
    iq = _as_int8_iq(raw)
    if iq.size == 0:
        raise RuntimeError(f"empty IQ payload: {iq_path}")

    repeat = max(1, int(args.repeat))
    if repeat > 1:
        iq = np.tile(iq, repeat)

    sdr.tx_cyclic_buffer = False
    sdr.tx(iq)

    try:
        time.sleep(max(1, int(args.timeout_seconds)))
    finally:
        try:
            if hasattr(sdr, "tx_destroy_buffer"):
                sdr.tx_destroy_buffer()
        except Exception:
            pass
    return 0


def main() -> int:
    args = _parse_args()
    sdr = _load_radio(args.uri)
    if args.mode == "rx":
        return _run_rx(sdr, args)
    return _run_tx(sdr, args)


if __name__ == "__main__":
    raise SystemExit(main())
