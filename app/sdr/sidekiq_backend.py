from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest
from app.sdr.soapy_utils import find_driver_devices


SIDEKIQ_FREQ_MIN = 70_000_000
SIDEKIQ_FREQ_MAX = 6_000_000_000
SIDEKIQ_MAX_SAMPLE_RATE = 61_440_000


class SidekiqBackend(SDRBackend):
    def list_devices(self) -> list[Device]:
        parsed = find_driver_devices("sidekiq")
        devices: list[Device] = []
        idx = 0
        for item in parsed:
            serial = item.get("serial") or None
            manufacturer = item.get("manufacturer", "Epiq Solutions")
            product = item.get("product", "Sidekiq")
            suffix = f" :: {serial}" if serial else ""
            label = f"{manufacturer} - {product}{suffix}"
            devices.append(
                Device(
                    id=f"sidekiq:{idx}",
                    driver="sidekiq",
                    label=label,
                    serial=serial,
                    freq_min_hz=SIDEKIQ_FREQ_MIN,
                    freq_max_hz=SIDEKIQ_FREQ_MAX,
                    max_sample_rate_sps=SIDEKIQ_MAX_SAMPLE_RATE,
                    notes="SoapySDR driver=sidekiq (CS16 native, gateway serves int8 IQ).",
                )
            )
            idx += 1
        return devices

    def start_stream(self, request: StreamRequest):
        worker = Path(__file__).with_name("soapy_worker.py")
        if not worker.exists():
            raise RuntimeError(f"soapy worker not found: {worker}")

        try:
            device_index = int(request.device_id.split(":", 1)[1])
        except Exception as exc:
            raise RuntimeError(f"invalid sidekiq device id: {request.device_id}") from exc

        cmd = [
            sys.executable,
            str(worker),
            "--driver",
            "sidekiq",
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
        raise RuntimeError("Sidekiq sweep backend is not implemented in sdr-gateway yet.")

    def stop_sweep(self, process) -> None:
        if process is None:
            return
