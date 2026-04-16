from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest, TxBurstRequest
from app.sdr.soapy_utils import find_driver_devices


BLADERF_FREQ_MIN = 47_000_000
BLADERF_FREQ_MAX = 6_000_000_000
BLADERF_MAX_SAMPLE_RATE = 61_440_000


class BladeRFBackend(SDRBackend):
    def list_devices(self) -> list[Device]:
        soapy_devices = find_driver_devices("bladerf")
        devices: list[Device] = []
        for idx, item in enumerate(soapy_devices):
            serial = item.get("serial") or None
            manufacturer = item.get("manufacturer", "Nuand")
            product = item.get("product", "bladeRF")
            suffix = f" :: {serial}" if serial else ""
            label = f"{manufacturer} - {product}{suffix}"
            devices.append(
                Device(
                    id=f"bladerf:{idx}",
                    driver="bladerf",
                    label=label,
                    serial=serial,
                    freq_min_hz=BLADERF_FREQ_MIN,
                    freq_max_hz=BLADERF_FREQ_MAX,
                    max_sample_rate_sps=BLADERF_MAX_SAMPLE_RATE,
                    notes="SoapySDR driver=bladerf (CS16 native, gateway serves int8 IQ).",
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
            raise RuntimeError(f"invalid bladeRF device id: {request.device_id}") from exc

        cmd = [
            sys.executable,
            str(worker),
            "--driver",
            "bladerf",
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
        raise RuntimeError("bladeRF sweep backend is not implemented in sdr-gateway yet.")

    def stop_sweep(self, process) -> None:
        if process is None:
            return

    def start_tx_burst(self, request: TxBurstRequest):
        worker = Path(__file__).with_name("soapy_worker.py")
        if not worker.exists():
            raise RuntimeError(f"soapy worker not found: {worker}")
        try:
            device_index = int(request.device_id.split(":", 1)[1])
        except Exception as exc:
            raise RuntimeError(f"invalid bladeRF device id: {request.device_id}") from exc

        fd, tx_path = tempfile.mkstemp(prefix="sdr_gateway_tx_", suffix=".iq")
        with os.fdopen(fd, "wb") as f:
            f.write(request.iq_i8)

        cmd = [
            sys.executable,
            str(worker),
            "--mode",
            "tx",
            "--driver",
            "bladerf",
            "--device-index",
            str(device_index),
            "--center-freq-hz",
            str(request.center_freq_hz),
            "--sample-rate-sps",
            str(request.sample_rate_sps),
            "--tx-gain-db",
            str(request.tx_gain_db),
            "--iq-file",
            tx_path,
            "--repeat",
            str(max(1, int(request.repeat))),
            "--timeout-seconds",
            str(max(1, int(request.timeout_seconds))),
        ]
        if request.baseband_filter_hz:
            cmd.extend(["--baseband-filter-hz", str(request.baseband_filter_hz)])

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            text=False,
        )
        setattr(process, "_tx_iq_path", tx_path)
        return process

    def stop_tx_burst(self, process) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        finally:
            tx_path = getattr(process, "_tx_iq_path", None)
            if tx_path:
                try:
                    os.unlink(tx_path)
                except OSError:
                    pass
