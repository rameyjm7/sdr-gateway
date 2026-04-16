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
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    kwargs = {"driver": "sidekiq", "card": str(args.card)}
    dev = SoapySDR.Device(kwargs)

    # Sidekiq gain is often fixed/AGC-controlled in plugin; set what we can safely.
    dev.setFrequency(SOAPY_SDR_RX, 0, float(args.center_freq_hz))
    dev.setSampleRate(SOAPY_SDR_RX, 0, float(args.sample_rate_sps))
    if args.baseband_filter_hz and args.baseband_filter_hz > 0:
        try:
            dev.setBandwidth(SOAPY_SDR_RX, 0, float(args.baseband_filter_hz))
        except Exception:
            # Not all drivers expose setBandwidth consistently.
            pass

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

            # Convert CS16 IQ to int8 IQ expected by current gateway clients.
            iq16 = rx_buf[: n * 2]
            iq8 = np.clip(np.rint(iq16.astype(np.float32) / 64.0), -128, 127).astype(np.int8, copy=False)
            out.write(iq8.tobytes())
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
