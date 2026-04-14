from __future__ import annotations

import shutil
import subprocess
from typing import Iterable

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest


HACKRF_FREQ_MIN = 1_000_000
HACKRF_FREQ_MAX = 6_000_000_000
HACKRF_MAX_SAMPLE_RATE = 20_000_000


def _cmd_available(command: str) -> bool:
    return shutil.which(command) is not None


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _parse_hackrf_serials(output: str) -> Iterable[str]:
    for line in output.splitlines():
        if "Serial Number:" in line:
            yield line.split("Serial Number:", 1)[1].strip()


class HackRFBackend(SDRBackend):
    def list_devices(self) -> list[Device]:
        if not _cmd_available("hackrf_info"):
            return []

        info = _run(["hackrf_info"])
        if info.returncode != 0:
            return []

        # Some hackrf_info builds write details to stderr; parse both streams.
        merged_output = f"{info.stdout}\n{info.stderr}"
        serials = list(_parse_hackrf_serials(merged_output))
        if not serials:
            # hackrf_info can fail to print serial on some versions; keep a generic entry.
            serials = [None]

        devices = []
        for idx, serial in enumerate(serials):
            label = "HackRF One"
            if serial:
                label = f"HackRF One ({serial[-6:]})"
            devices.append(
                Device(
                    id=f"hackrf:{idx}",
                    driver="hackrf",
                    label=label,
                    serial=serial,
                    freq_min_hz=HACKRF_FREQ_MIN,
                    freq_max_hz=HACKRF_FREQ_MAX,
                    max_sample_rate_sps=HACKRF_MAX_SAMPLE_RATE,
                    notes="8-bit I/Q (CS8), USB 2.0; practical stable rates depend on host.",
                )
            )
        return devices

    def start_stream(self, request: StreamRequest):
        if not _cmd_available("hackrf_transfer"):
            raise RuntimeError("hackrf_transfer not found in PATH")

        cmd = [
            "hackrf_transfer",
            "-r",
            "-",
            "-f",
            str(request.center_freq_hz),
            "-s",
            str(request.sample_rate_sps),
            "-a",
            "1" if request.amp_enable else "0",
            "-l",
            str(request.lna_gain_db),
            "-g",
            str(request.vga_gain_db),
        ]
        if request.baseband_filter_hz:
            cmd.extend(["-b", str(request.baseband_filter_hz)])

        # stdout carries raw interleaved int8 IQ bytes.
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
        if not _cmd_available("hackrf_sweep"):
            raise RuntimeError("hackrf_sweep not found in PATH")
        if request.start_freq_hz >= request.stop_freq_hz:
            raise ValueError("start_freq_hz must be lower than stop_freq_hz")

        # hackrf_sweep expects MHz ranges for -f, e.g. 2400:2483.
        start_mhz = request.start_freq_hz // 1_000_000
        stop_mhz = request.stop_freq_hz // 1_000_000
        cmd = [
            "hackrf_sweep",
            "-f",
            f"{start_mhz}:{stop_mhz}",
            "-w",
            str(request.bin_width_hz),
            "-a",
            "1" if request.amp_enable else "0",
            "-l",
            str(request.lna_gain_db),
            "-g",
            str(request.vga_gain_db),
        ]

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def stop_sweep(self, process) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
