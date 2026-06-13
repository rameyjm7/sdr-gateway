from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

import numpy as np

try:
    import SoapySDR  # type: ignore
    from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_RX  # type: ignore
except Exception as exc:  # pragma: no cover - runtime dependency
    # Fallback when running inside a venv but SoapySDR Python bindings were
    # installed into /usr/local system site-packages.
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    fallback = Path(f"/usr/local/lib/python{ver}/site-packages")
    if fallback.exists() and str(fallback) not in sys.path:
        sys.path.append(str(fallback))
    try:
        import SoapySDR  # type: ignore
        from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_RX  # type: ignore
    except Exception:
        print(f"SoapySDR import failed: {exc}", file=sys.stderr)
        print(f"PYTHONPATH={os.getenv('PYTHONPATH', '')}", file=sys.stderr)
        raise


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sidekiq IQ streaming worker (stdout int8 IQ).")
    p.add_argument("--center-freq-hz", type=int, required=True)
    p.add_argument("--sample-rate-sps", type=int, required=True)
    p.add_argument("--baseband-filter-hz", type=int, default=0)
    p.add_argument("--duration-seconds", type=int, default=0)
    p.add_argument("--num-samples", type=int, default=0)
    p.add_argument("--card", type=int, default=0)
    p.add_argument("--lna-gain-db", type=int, default=0)
    p.add_argument("--vga-gain-db", type=int, default=0)
    return p.parse_args()


def _range_bounds(rng, default_min: float, default_max: float) -> tuple[float, float]:
    try:
        if isinstance(rng, (tuple, list)) and len(rng) >= 2:
            return float(rng[0]), float(rng[1])
        if hasattr(rng, "minimum") and hasattr(rng, "maximum"):
            min_fn = getattr(rng, "minimum")
            max_fn = getattr(rng, "maximum")
            if callable(min_fn) and callable(max_fn):
                return float(min_fn()), float(max_fn())
            return float(min_fn), float(max_fn)
        if hasattr(rng, "min") and hasattr(rng, "max"):
            return float(getattr(rng, "min")), float(getattr(rng, "max"))
    except Exception:
        pass
    return float(default_min), float(default_max)


def _clip_gain(dev, name: str | None, value: float) -> float:
    try:
        if name:
            lo, hi = _range_bounds(dev.getGainRange(SOAPY_SDR_RX, 0, name), 0.0, 76.0)
        else:
            lo, hi = _range_bounds(dev.getGainRange(SOAPY_SDR_RX, 0), 0.0, 76.0)
    except Exception:
        lo, hi = 0.0, 76.0
    if hi <= lo:
        # SoapySidekiq can report 0..0 even though libsidekiq accepts 0..76.
        lo, hi = 0.0, 76.0
    return float(min(max(value, lo), hi))


def _set_gain(dev, lna_gain_db: int, vga_gain_db: int) -> None:
    try:
        dev.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    try:
        names = list(dev.listGains(SOAPY_SDR_RX, 0))
    except Exception:
        names = []
    by_lower = {str(name).lower(): str(name) for name in names}
    total = float(lna_gain_db + vga_gain_db)
    for preferred, value in (
        ("lna", float(lna_gain_db)),
        ("rx", total),
        ("tuner", total),
        ("gain", total),
    ):
        actual = by_lower.get(preferred)
        if not actual:
            continue
        try:
            dev.setGain(SOAPY_SDR_RX, 0, actual, _clip_gain(dev, actual, value))
            return
        except Exception:
            pass
    try:
        dev.setGain(SOAPY_SDR_RX, 0, _clip_gain(dev, None, total))
    except Exception:
        pass


def main() -> int:
    args = _parse_args()
    kwargs = {"driver": "sidekiq", "card": str(args.card)}
    dev = SoapySDR.Device(kwargs)

    dev.setFrequency(SOAPY_SDR_RX, 0, float(args.center_freq_hz))
    dev.setSampleRate(SOAPY_SDR_RX, 0, float(args.sample_rate_sps))
    if args.baseband_filter_hz and args.baseband_filter_hz > 0:
        try:
            dev.setBandwidth(SOAPY_SDR_RX, 0, float(args.baseband_filter_hz))
        except Exception:
            # Not all drivers expose setBandwidth consistently.
            pass
    _set_gain(dev, args.lna_gain_db, args.vga_gain_db)

    stream = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    dev.activateStream(stream)

    start_ts = time.time()
    max_samples = int(args.num_samples) if args.num_samples > 0 else 0
    duration_s = int(args.duration_seconds) if args.duration_seconds > 0 else 0
    produced_samples = 0

    # numElems, one channel, CS16 -> int16 interleaved IQ uses 2 ints per sample.
    chunk_samples = 16_384
    rx_buf = np.empty(chunk_samples * 2, dtype=np.int16)
    out = sys.stdout.buffer

    try:
        while True:
            if duration_s and (time.time() - start_ts) >= duration_s:
                break
            if max_samples and produced_samples >= max_samples:
                break

            read_count = chunk_samples
            if max_samples:
                read_count = min(read_count, max_samples - produced_samples)
                if read_count <= 0:
                    break

            result = dev.readStream(stream, [rx_buf], int(read_count), timeoutUs=200_000)
            n = int(getattr(result, "ret", result))
            if n <= 0:
                continue

            # Sidekiq delivers signed interleaved CS16 IQ.
            # Keep the native int16 payload so Sidekiq-aware clients can
            # preserve the full dynamic range instead of forcing int8.
            iq16 = rx_buf[: n * 2]
            out.write(iq16.tobytes())
            produced_samples += n
    finally:
        try:
            dev.deactivateStream(stream)
        except Exception:
            pass
        try:
            dev.closeStream(stream)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
