from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest
from app.sdr.soapy_utils import find_driver_devices


RTLSDR_FREQ_MIN = 24_000_000
RTLSDR_FREQ_MAX = 1_766_000_000
RTLSDR_MAX_SAMPLE_RATE = 3_200_000


class RTLSDRBackend(SDRBackend):
    def list_devices(self) -> list[Device]:
        soapy_devices = find_driver_devices("rtlsdr")
        devices: list[Device] = []
        for idx, item in enumerate(soapy_devices):
            serial = item.get("serial") or None
            manufacturer = item.get("manufacturer", "Realtek")
            product = item.get("product", "RTL-SDR")
            tuner = item.get("tuner", "")
            suffix = f" :: {serial}" if serial else ""
            label = f"{manufacturer} - {product}{suffix}"
            notes = "SoapySDR driver=rtlsdr (CS16 native, gateway serves int8 IQ)."
            if tuner:
                notes = f"{notes} Tuner: {tuner}."
            devices.append(
                Device(
                    id=f"rtlsdr:{idx}",
                    driver="rtlsdr",
                    label=label,
                    serial=serial,
                    freq_min_hz=RTLSDR_FREQ_MIN,
                    freq_max_hz=RTLSDR_FREQ_MAX,
                    max_sample_rate_sps=RTLSDR_MAX_SAMPLE_RATE,
                    notes=notes,
                )
            )
        return devices

    def start_stream(self, request: StreamRequest):
        worker = Path(__file__).with_name("soapy_worker.py")
        if not worker.exists():
            raise RuntimeError(f"soapy worker not found: {worker}")
        try:
            device_index = int(request.device_id.split(":", 1)[1])
        except Exception as exc:
            raise RuntimeError(f"invalid rtlsdr device id: {request.device_id}") from exc

        cmd = [
            sys.executable,
            str(worker),
            "--driver",
            "rtlsdr",
            "--device-index",
            str(device_index),
            "--center-freq-hz",
            str(request.center_freq_hz),
            "--sample-rate-sps",
            str(request.sample_rate_sps),
            "--lna-gain-db",
            str(request.lna_gain_db),
            "--vga-gain-db",
            str(request.vga_gain_db),
        ]
        if request.baseband_filter_hz:
            cmd.extend(["--baseband-filter-hz", str(request.baseband_filter_hz)])
        if request.duration_seconds:
            cmd.extend(["--duration-seconds", str(request.duration_seconds)])
        if request.num_samples:
            cmd.extend(["--num-samples", str(request.num_samples)])

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            text=False,
        )

    def stop_stream(self, process) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def start_sweep(self, request: SweepRequest):
        raise RuntimeError("RTL-SDR sweep backend is not implemented in sdr-gateway yet.")

    def stop_sweep(self, process) -> None:
        if process is None:
            return
