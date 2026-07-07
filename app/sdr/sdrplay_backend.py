from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest, TxBurstRequest
from app.sdr.controlled_process import ControlledStreamProcess
from app.sdr.soapy_utils import find_driver_devices
from app.sdr.usb_utils import lsusb_devices


# RSP2 can be useful down into LF/VLF with the right input path.  Keep the
# gateway permissive here so low-frequency protocol jobs can own the SDRplay
# through the same scheduler/stream API as the 2.4 GHz stacks.
SDRPLAY_FREQ_MIN = 1_000
SDRPLAY_FREQ_MAX = 2_000_000_000
SDRPLAY_MAX_SAMPLE_RATE = 10_000_000
SDRPLAY_USB_VIDPID_PREFIX = "1df7:"


class SDRplayBackend(SDRBackend):
    def list_devices(self) -> list[Device]:
        soapy_devices = find_driver_devices("sdrplay")
        devices: list[Device] = []
        for idx, item in enumerate(soapy_devices):
            serial = item.get("serial") or None
            manufacturer = item.get("manufacturer", "SDRplay")
            product = item.get("label") or item.get("product", "RSP")
            suffix = f" :: {serial}" if serial and serial not in product else ""
            label = f"{manufacturer} - {product}{suffix}"
            devices.append(
                Device(
                    id=f"sdrplay:{idx}",
                    driver="sdrplay",
                    label=label,
                    serial=serial,
                    freq_min_hz=SDRPLAY_FREQ_MIN,
                    freq_max_hz=SDRPLAY_FREQ_MAX,
                    max_sample_rate_sps=SDRPLAY_MAX_SAMPLE_RATE,
                    notes="SoapySDR driver=sdrplay (CS16 native, gateway serves int8 IQ).",
                )
            )

        if devices:
            return devices

        # If SoapySDRPlay discovery is wedged, still expose a presence-only row
        # when Linux can see an RSP. Streaming may need SDRPLAY_SERIAL.
        sdrplay_usb = [
            desc
            for vidpid, desc in lsusb_devices()
            if vidpid.startswith(SDRPLAY_USB_VIDPID_PREFIX)
        ]
        if not sdrplay_usb:
            return []
        serial = os.getenv("SDRPLAY_SERIAL", "").strip() or None
        label = sdrplay_usb[0] or "SDRplay RSP"
        notes = "SDRplay USB device present; SoapySDR driver probe did not return details."
        if serial:
            notes = f"{notes} Using SDRPLAY_SERIAL={serial} for stream opens."
        return [
            Device(
                id="sdrplay:0",
                driver="sdrplay",
                label=label,
                serial=serial,
                freq_min_hz=SDRPLAY_FREQ_MIN,
                freq_max_hz=SDRPLAY_FREQ_MAX,
                max_sample_rate_sps=SDRPLAY_MAX_SAMPLE_RATE,
                notes=notes,
            )
        ]

    def start_stream(self, request: StreamRequest):
        worker = Path(__file__).with_name("soapy_worker.py")
        if not worker.exists():
            raise RuntimeError(f"soapy worker not found: {worker}")
        try:
            device_index = int(request.device_id.split(":", 1)[1])
        except Exception as exc:
            raise RuntimeError(f"invalid sdrplay device id: {request.device_id}") from exc

        cmd = [
            sys.executable,
            str(worker),
            "--driver",
            "sdrplay",
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
        if request.rx_channels:
            cmd.extend(["--rx-channels", ",".join(str(ch) for ch in request.rx_channels)])
        if request.baseband_filter_hz:
            cmd.extend(["--baseband-filter-hz", str(request.baseband_filter_hz)])
        if request.duration_seconds:
            cmd.extend(["--duration-seconds", str(request.duration_seconds)])
        if request.num_samples:
            cmd.extend(["--num-samples", str(request.num_samples)])

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            text=False,
        )
        return ControlledStreamProcess(process)

    def retune_stream(self, process, request: StreamRequest) -> bool:
        retune = getattr(process, "retune", None)
        return bool(retune and retune(request))

    def stop_stream(self, process) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def start_sweep(self, request: SweepRequest):
        worker = Path(__file__).with_name("soapy_sweep_worker.py")
        if not worker.exists():
            raise RuntimeError(f"soapy sweep worker not found: {worker}")
        try:
            device_index = int(request.device_id.split(":", 1)[1])
        except Exception as exc:
            raise RuntimeError(f"invalid sdrplay device id: {request.device_id}") from exc

        sample_rate_sps = SDRPLAY_MAX_SAMPLE_RATE
        cmd = [
            sys.executable,
            str(worker),
            "--driver",
            "sdrplay",
            "--device-index",
            str(device_index),
            "--start-freq-hz",
            str(request.start_freq_hz),
            "--stop-freq-hz",
            str(request.stop_freq_hz),
            "--bin-width-hz",
            str(request.bin_width_hz),
            "--sample-rate-sps",
            str(sample_rate_sps),
            "--baseband-filter-hz",
            str(sample_rate_sps),
            "--lna-gain-db",
            str(request.lna_gain_db),
            "--vga-gain-db",
            str(request.vga_gain_db),
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def stop_sweep(self, process) -> None:
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def start_tx_burst(self, request: TxBurstRequest):
        raise RuntimeError("TX is not supported for SDRplay devices.")

    def stop_tx_burst(self, process) -> None:
        if process is None:
            return
