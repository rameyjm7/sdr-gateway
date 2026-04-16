from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Iterable

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest, TxBurstRequest


HACKRF_FREQ_MIN = 1_000_000
HACKRF_FREQ_MAX = 6_000_000_000
HACKRF_MAX_SAMPLE_RATE = 20_000_000


def _cmd_available(command: str) -> bool:
    return shutil.which(command) is not None


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    # Some hackrf tools can emit non-UTF8 bytes in stderr/stdout on certain hosts.
    # Decode defensively so device listing can't crash the API.
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _parse_hackrf_serials(output: str) -> Iterable[str]:
    for line in output.splitlines():
        if "Serial Number:" in line:
            yield line.split("Serial Number:", 1)[1].strip()

def _count_hackrf_lsusb() -> int:
    if not _cmd_available("lsusb"):
        return 0
    result = _run(["lsusb"])
    if result.returncode != 0:
        return 0
    merged_output = f"{result.stdout}\n{result.stderr}"
    # HackRF VID:PID
    return sum(1 for line in merged_output.splitlines() if "1d50:6089" in line.lower())


def _nearest_step(value: int, step: int, lo: int, hi: int) -> int:
    clamped = min(max(value, lo), hi)
    return int(round(clamped / step) * step)


def _cleanup_tx_file(process: object) -> None:
    tx_file = getattr(process, "_tx_iq_path", None)
    if not tx_file:
        return
    try:
        os.unlink(tx_file)
    except OSError:
        pass


class HackRFBackend(SDRBackend):
    def list_devices(self) -> list[Device]:
        if not _cmd_available("hackrf_info"):
            usb_count = _count_hackrf_lsusb()
            if usb_count <= 0:
                return []
            serials: list[str | None] = [None] * usb_count
            return [
                Device(
                    id=f"hackrf:{idx}",
                    driver="hackrf",
                    label="HackRF One",
                    serial=serial,
                    freq_min_hz=HACKRF_FREQ_MIN,
                    freq_max_hz=HACKRF_FREQ_MAX,
                    max_sample_rate_sps=HACKRF_MAX_SAMPLE_RATE,
                    notes="8-bit I/Q (CS8), USB 2.0; practical stable rates depend on host.",
                )
                for idx, serial in enumerate(serials)
            ]

        info = _run(["hackrf_info"])
        # Some hackrf_info builds write details to stderr; parse both streams.
        merged_output = f"{info.stdout}\n{info.stderr}"
        # "No HackRF boards found" is definitive; keep list empty.
        if "No HackRF boards found" in merged_output:
            return []

        serials = list(_parse_hackrf_serials(merged_output))
        if not serials and ("Found HackRF" in merged_output or info.returncode == 0):
            # Some host/tooling combos fail to print serial reliably (or exit non-zero
            # despite detecting a device). Keep a generic entry so UI can still select it.
            serials = [None]
        elif not serials:
            usb_count = _count_hackrf_lsusb()
            if usb_count > 0:
                serials = [None] * usb_count
            else:
                return []

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

        # HackRF gain constraints: LNA in steps of 8 dB (0..40), VGA in steps of 2 dB (0..62).
        lna = _nearest_step(request.lna_gain_db, step=8, lo=0, hi=40)
        vga = _nearest_step(request.vga_gain_db, step=2, lo=0, hi=62)

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
            str(lna),
            "-g",
            str(vga),
        ]
        if request.baseband_filter_hz:
            cmd.extend(["-b", str(request.baseband_filter_hz)])
        # Support finite captures: explicit num_samples wins, else derive from duration.
        num_samples = request.num_samples
        if num_samples is None and request.duration_seconds:
            num_samples = int(request.duration_seconds * request.sample_rate_sps)
        if num_samples:
            cmd.extend(["-n", str(num_samples)])

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
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def stop_sweep(self, process) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def start_tx_burst(self, request: TxBurstRequest):
        if not _cmd_available("hackrf_transfer"):
            raise RuntimeError("hackrf_transfer not found in PATH")

        tx_gain = min(max(int(request.tx_gain_db), 0), 47)

        payload = request.iq_i8 * max(1, int(request.repeat))
        fd, path = tempfile.mkstemp(prefix="sdr_gateway_tx_", suffix=".iq")
        with os.fdopen(fd, "wb") as f:
            f.write(payload)

        cmd = [
            "hackrf_transfer",
            "-t",
            path,
            "-f",
            str(request.center_freq_hz),
            "-s",
            str(request.sample_rate_sps),
            "-a",
            "1" if request.amp_enable else "0",
            "-x",
            str(tx_gain),
        ]
        if request.baseband_filter_hz:
            cmd.extend(["-b", str(request.baseband_filter_hz)])

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        setattr(process, "_tx_iq_path", path)
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
            _cleanup_tx_file(process)
