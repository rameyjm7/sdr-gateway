from __future__ import annotations

import argparse
import csv
import sys
import time

import numpy as np

from app.sdr.soapy_worker import (
    SOAPY_SDR_CS16,
    SOAPY_SDR_RX,
    _apply_driver_gain,
    _select_device_kwargs,
)

try:
    import SoapySDR  # type: ignore
except Exception as exc:  # pragma: no cover - runtime dependency
    print(f"SoapySDR import failed: {exc}", file=sys.stderr)
    raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SoapySDR FFT sweep worker that emits hackrf_sweep-style CSV.")
    parser.add_argument("--driver", required=True)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--start-freq-hz", type=int, required=True)
    parser.add_argument("--stop-freq-hz", type=int, required=True)
    parser.add_argument("--bin-width-hz", type=int, required=True)
    parser.add_argument("--sample-rate-sps", type=int, default=10_000_000)
    parser.add_argument("--baseband-filter-hz", type=int, default=10_000_000)
    parser.add_argument("--lna-gain-db", type=int, default=0)
    parser.add_argument("--vga-gain-db", type=int, default=35)
    parser.add_argument("--average-count", type=int, default=4)
    parser.add_argument("--chunk-samples", type=int, default=65_536)
    return parser.parse_args()


def _setup_device(args: argparse.Namespace):
    kwargs = _select_device_kwargs(str(args.driver), int(args.device_index))
    dev = SoapySDR.Device(kwargs)
    dev.setSampleRate(SOAPY_SDR_RX, 0, float(args.sample_rate_sps))
    try:
        dev.setBandwidth(SOAPY_SDR_RX, 0, float(args.baseband_filter_hz or args.sample_rate_sps))
    except Exception:
        pass
    _apply_driver_gain(dev, str(args.driver), int(args.lna_gain_db), int(args.vga_gain_db))
    stream = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    dev.activateStream(stream)
    return dev, stream


def _close_device(dev, stream) -> None:
    try:
        dev.deactivateStream(stream)
    except Exception:
        pass
    try:
        dev.closeStream(stream)
    except Exception:
        pass


def _window_centers(start_hz: int, stop_hz: int, sample_rate_sps: int) -> list[int]:
    usable_hz = max(1, int(float(sample_rate_sps) * 0.90))
    half = usable_hz // 2
    centers: list[int] = []
    cursor = int(start_hz)
    while cursor < int(stop_hz):
        center = cursor + half
        center = max(center, int(start_hz) + half)
        center = min(center, int(stop_hz) - half)
        if not centers or center != centers[-1]:
            centers.append(center)
        next_cursor = center + half
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    if not centers:
        centers.append((int(start_hz) + int(stop_hz)) // 2)
    return centers


def _read_iq(dev, stream, sample_count: int) -> np.ndarray:
    buf = np.empty(int(sample_count) * 2, dtype=np.int16)
    chunks: list[np.ndarray] = []
    remaining = int(sample_count)
    while remaining > 0:
        read_count = min(remaining, sample_count)
        result = dev.readStream(stream, [buf], int(read_count), timeoutUs=300_000)
        n = int(getattr(result, "ret", result))
        if n <= 0:
            continue
        raw = buf[: n * 2].astype(np.float32) / 32768.0
        chunks.append((raw[0::2] + 1j * raw[1::2]).astype(np.complex64, copy=False))
        remaining -= n
    return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.complex64)


def _power_spectrum_db(iq: np.ndarray, sample_rate_sps: int) -> tuple[np.ndarray, np.ndarray]:
    if iq.size < 4096:
        return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32)
    nfft = min(131_072, 1 << int(np.floor(np.log2(iq.size))))
    work = iq[-nfft:] - np.mean(iq[-nfft:])
    window = np.hanning(nfft).astype(np.float32)
    spectrum = np.fft.fftshift(np.fft.fft(work * window))
    power = 20.0 * np.log10((np.abs(spectrum) / float(nfft)) + 1e-12)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / float(sample_rate_sps)))
    return freqs.astype(np.float32), power.astype(np.float32)


def _window_bin_values(
    freqs: np.ndarray,
    power_db: np.ndarray,
    *,
    center_hz: int,
    hz_low: int,
    hz_high: int,
    bin_width_hz: int,
) -> list[float]:
    values: list[float] = []
    for low in range(int(hz_low), int(hz_high), int(bin_width_hz)):
        high = min(int(hz_high), low + int(bin_width_hz))
        offset_low = float(low - center_hz)
        offset_high = float(high - center_hz)
        mask = (freqs >= offset_low) & (freqs < offset_high)
        if not np.any(mask):
            values.append(-120.0)
            continue
        values.append(round(float(np.max(power_db[mask])), 1))
    return values


def _emit_row(writer: csv.writer, hz_low: int, hz_high: int, bin_width_hz: int, sample_count: int, values: list[float]) -> None:
    now = time.gmtime()
    writer.writerow(
        [
            time.strftime("%Y-%m-%d", now),
            time.strftime("%H:%M:%S", now),
            int(hz_low),
            int(hz_high),
            int(bin_width_hz),
            int(sample_count),
            *[f"{value:.1f}" for value in values],
        ]
    )
    sys.stdout.flush()


def main() -> int:
    args = _parse_args()
    if int(args.start_freq_hz) >= int(args.stop_freq_hz):
        raise RuntimeError("start_freq_hz must be lower than stop_freq_hz")
    if int(args.bin_width_hz) <= 0:
        raise RuntimeError("bin_width_hz must be positive")

    dev, stream = _setup_device(args)
    writer = csv.writer(sys.stdout)
    try:
        centers = _window_centers(int(args.start_freq_hz), int(args.stop_freq_hz), int(args.sample_rate_sps))
        usable_hz = int(float(args.sample_rate_sps) * 0.90)
        half = usable_hz // 2
        while True:
            for center_hz in centers:
                dev.setFrequency(SOAPY_SDR_RX, 0, float(center_hz))
                time.sleep(0.02)
                spectra: list[np.ndarray] = []
                freqs: np.ndarray | None = None
                total_samples = 0
                for _ in range(max(1, int(args.average_count))):
                    iq = _read_iq(dev, stream, int(args.chunk_samples))
                    total_samples += int(iq.size)
                    freq_axis, power = _power_spectrum_db(iq, int(args.sample_rate_sps))
                    if power.size:
                        freqs = freq_axis
                        spectra.append(power)
                if freqs is None or not spectra:
                    continue
                power_db = np.median(np.vstack(spectra), axis=0)
                hz_low = max(int(args.start_freq_hz), int(center_hz - half))
                hz_high = min(int(args.stop_freq_hz), int(center_hz + half))
                hz_low = (hz_low // int(args.bin_width_hz)) * int(args.bin_width_hz)
                hz_high = ((hz_high + int(args.bin_width_hz) - 1) // int(args.bin_width_hz)) * int(args.bin_width_hz)
                hz_low = max(int(args.start_freq_hz), hz_low)
                hz_high = min(int(args.stop_freq_hz), hz_high)
                if hz_high <= hz_low:
                    continue
                values = _window_bin_values(
                    freqs,
                    power_db,
                    center_hz=int(center_hz),
                    hz_low=int(hz_low),
                    hz_high=int(hz_high),
                    bin_width_hz=int(args.bin_width_hz),
                )
                _emit_row(writer, int(hz_low), int(hz_high), int(args.bin_width_hz), total_samples, values)
    finally:
        _close_device(dev, stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
