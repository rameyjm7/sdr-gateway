from __future__ import annotations

import asyncio
import base64
import math
import os
import time

import pytest
import requests
import websockets
import numpy as np


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _generate_two_tone_iq(
    sample_rate_sps: int,
    tone_hz_1: float,
    tone_hz_2: float,
    seconds: float = 0.05,
    amplitude: float = 100.0,
) -> bytes:
    n = max(16, int(sample_rate_sps * seconds))
    out = bytearray(n * 2)
    w1 = 2.0 * math.pi * tone_hz_1 / sample_rate_sps
    w2 = 2.0 * math.pi * tone_hz_2 / sample_rate_sps
    for i in range(n):
        c = math.cos(w1 * i) + math.cos(w2 * i)
        s = math.sin(w1 * i) + math.sin(w2 * i)
        iv = int(max(-127, min(127, round(0.5 * amplitude * c))))
        qv = int(max(-127, min(127, round(0.5 * amplitude * s))))
        out[2 * i] = iv & 0xFF
        out[2 * i + 1] = qv & 0xFF
    return bytes(out)


def _complex_from_iq_i8(raw: bytes, max_samples: int = 120_000) -> list[complex]:
    nbytes = min(len(raw), max_samples * 2)
    nbytes -= nbytes % 2
    samples: list[complex] = []
    for i in range(0, nbytes, 2):
        iv = raw[i] if raw[i] < 128 else raw[i] - 256
        qv = raw[i + 1] if raw[i + 1] < 128 else raw[i + 1] - 256
        samples.append(complex(iv, qv))
    return samples


def _is_power_of_two(v: int) -> bool:
    return v > 0 and (v & (v - 1)) == 0


def _detect_two_tone_signature(
    samples: list[complex],
    sample_rate_sps: int,
    expected_spacing_hz: float,
    spacing_tol_hz: float = 35_000.0,
    min_peak_over_floor_db: float = 7.0,
) -> tuple[bool, str]:
    if len(samples) < 4096:
        return False, f"insufficient samples ({len(samples)})"

    n = min(len(samples), 131072)
    if not _is_power_of_two(n):
        n = 1 << (n.bit_length() - 1)
    x = np.asarray(samples[:n], dtype=np.complex64)
    if x.size < 4096:
        return False, f"insufficient FFT size ({x.size})"

    # Robust PSD estimate; tone absolute offset can drift, spacing should remain stable.
    x = x - np.mean(x)
    win = np.hanning(x.size).astype(np.float32)
    xw = x * win
    spec = np.fft.fftshift(np.fft.fft(xw))
    pwr = np.abs(spec) ** 2 + 1e-12
    pwr_db = 10.0 * np.log10(pwr)
    freq = np.fft.fftshift(np.fft.fftfreq(x.size, d=1.0 / sample_rate_sps))

    usable = np.abs(freq) <= (0.48 * sample_rate_sps)
    p = pwr_db[usable]
    f = freq[usable]
    floor_db = float(np.median(p))

    # Local maxima as tone candidates.
    peak_idx = np.where((p[1:-1] > p[:-2]) & (p[1:-1] >= p[2:]))[0] + 1
    if peak_idx.size < 2:
        return False, f"no spectral peaks; floor={floor_db:.1f} dB"

    # Keep strongest peaks for pair search.
    order = peak_idx[np.argsort(p[peak_idx])[::-1]]
    strong = [int(i) for i in order if (float(p[i]) - floor_db) >= min_peak_over_floor_db][:80]
    if len(strong) < 2:
        top_db = float(p[order[0]]) if order.size else floor_db
        return False, f"peaks too weak; top-floor={top_db - floor_db:.1f} dB"

    best_spacing_err = 1e18
    best = None
    for a in range(len(strong)):
        fa = float(f[strong[a]])
        pa = float(p[strong[a]])
        for b in range(a + 1, len(strong)):
            fb = float(f[strong[b]])
            pb = float(p[strong[b]])
            spacing = abs(fb - fa)
            err = abs(spacing - expected_spacing_hz)
            if err < best_spacing_err:
                best_spacing_err = err
                best = (spacing, fa, fb, pa, pb)

    if best is None:
        return False, "no candidate pairs"

    spacing, fa, fb, pa, pb = best
    ok = best_spacing_err <= spacing_tol_hz
    msg = (
        f"spacing={spacing:.1f} Hz (target {expected_spacing_hz:.1f} +/- {spacing_tol_hz:.1f}), "
        f"err={best_spacing_err:.1f}, peaks=({fa:.1f}Hz,{pa-floor_db:.1f}dB over floor) "
        f"({fb:.1f}Hz,{pb-floor_db:.1f}dB over floor), floor={floor_db:.1f} dB"
    )
    return ok, msg


async def _recv_ws_bytes(ws_url: str, target_bytes: int, timeout_s: float = 8.0) -> bytes:
    buf = bytearray()
    started = time.time()
    async with websockets.connect(ws_url, max_size=None) as ws:
        while len(buf) < target_bytes and (time.time() - started) < timeout_s:
            remain = max(0.2, timeout_s - (time.time() - started))
            try:
                chunk = await asyncio.wait_for(ws.recv(), timeout=remain)
            except asyncio.TimeoutError:
                break
            if isinstance(chunk, bytes):
                buf.extend(chunk)
            else:
                break
    return bytes(buf)


@pytest.mark.skipif(
    not _env_bool("RUN_SDR_HW_TESTS", default=False),
    reason="Set RUN_SDR_HW_TESTS=1 to run hardware functional tests.",
)
def test_hackrf_tx_bladerf_rx_two_tone():
    api_base = os.getenv("SDR_GATEWAY_API_BASE", "http://127.0.0.1:8080").rstrip("/")
    ws_base = os.getenv("SDR_GATEWAY_WS_BASE", "ws://127.0.0.1:8080").rstrip("/")
    token = os.getenv("SDR_GATEWAY_API_TOKEN", "").strip()
    if not token:
        pytest.skip("SDR_GATEWAY_API_TOKEN is required for functional hardware test.")

    headers = {"Authorization": f"Bearer {token}"}

    devices_resp = requests.get(f"{api_base}/devices", headers=headers, timeout=10)
    devices_resp.raise_for_status()
    devices = devices_resp.json()
    if not devices:
        pytest.skip("No SDR devices available from gateway.")

    tx_device = os.getenv("TEST_TX_DEVICE_ID", "").strip()
    rx_device = os.getenv("TEST_RX_DEVICE_ID", "").strip()
    if not tx_device:
        tx_device = next((d["id"] for d in devices if str(d["id"]).startswith("hackrf:")), "")
    if not rx_device:
        rx_device = next((d["id"] for d in devices if str(d["id"]).startswith("bladerf:")), "")
    if not tx_device or not rx_device:
        pytest.skip("This test needs HackRF TX and bladeRF RX. Set TEST_TX_DEVICE_ID/TEST_RX_DEVICE_ID if needed.")

    center_freq_hz = int(os.getenv("TEST_TXRX_CENTER_FREQ_HZ", "751000000"))
    sample_rate_sps = int(os.getenv("TEST_TXRX_SAMPLE_RATE_SPS", "2000000"))
    tone_hz_1 = float(os.getenv("TEST_TX_TONE_HZ_1", "120000"))
    tone_hz_2 = float(os.getenv("TEST_TX_TONE_HZ_2", "260000"))

    iq_payload = _generate_two_tone_iq(
        sample_rate_sps=sample_rate_sps,
        tone_hz_1=tone_hz_1,
        tone_hz_2=tone_hz_2,
        seconds=0.05,
        amplitude=100.0,
    )
    tx_b64 = base64.b64encode(iq_payload).decode("ascii")

    rx_start_payload = {
        "device_id": rx_device,
        "center_freq_hz": center_freq_hz,
        "sample_rate_sps": sample_rate_sps,
        "lna_gain_db": 16,
        "vga_gain_db": 24,
        "amp_enable": False,
        "baseband_filter_hz": sample_rate_sps,
        "duration_seconds": 8,
    }
    tx_start_payload = {
        "device_id": tx_device,
        "center_freq_hz": center_freq_hz,
        "sample_rate_sps": sample_rate_sps,
        "tx_gain_db": 30,
        "amp_enable": False,
        "baseband_filter_hz": sample_rate_sps,
        "iq_i8_b64": tx_b64,
        "repeat": 60,
        "timeout_seconds": 8,
    }

    stream_id = None
    tx_id = None
    try:
        rx_resp = requests.post(f"{api_base}/streams/start", headers=headers, json=rx_start_payload, timeout=15)
        rx_resp.raise_for_status()
        stream_id = rx_resp.json()["stream_id"]

        tx_resp = requests.post(f"{api_base}/tx/start", headers=headers, json=tx_start_payload, timeout=15)
        tx_resp.raise_for_status()
        tx_id = tx_resp.json()["tx_id"]

        ws_url = f"{ws_base}/ws/iq/{stream_id}?token={token}"
        raw = asyncio.run(_recv_ws_bytes(ws_url, target_bytes=400_000, timeout_s=8.0))
        assert len(raw) >= 40_000, f"Too little RX IQ captured: {len(raw)} bytes"

        samples = _complex_from_iq_i8(raw, max_samples=120_000)
        assert len(samples) >= 10_000, f"Too few complex samples for detection: {len(samples)}"

        expected_spacing = abs(tone_hz_2 - tone_hz_1)
        ok, detail = _detect_two_tone_signature(
            samples=samples,
            sample_rate_sps=sample_rate_sps,
            expected_spacing_hz=expected_spacing,
            spacing_tol_hz=35_000.0,
            min_peak_over_floor_db=7.0,
        )
        assert ok, f"Two-tone TX signature not detected: {detail}"
    finally:
        if tx_id:
            requests.post(f"{api_base}/tx/{tx_id}/stop", headers=headers, timeout=10)
        if stream_id:
            requests.post(f"{api_base}/streams/{stream_id}/stop", headers=headers, timeout=10)
