from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest, TxBurstRequest


ANTSDR_E200_URI = os.getenv("ANTSDR_E200_URI", "ip:192.168.1.10").strip() or "ip:192.168.1.10"
ANTSDR_E200_FREQ_MIN = 70_000_000
ANTSDR_E200_FREQ_MAX = 6_000_000_000
ANTSDR_E200_MAX_SAMPLE_RATE = 61_440_000
ANTSDR_E200_IIO_PORT = int(os.getenv("ANTSDR_E200_IIO_PORT", "30431"))


def _split_device_index(device_id: str) -> int:
    try:
        return int(device_id.split(":", 1)[1])
    except Exception as exc:
        raise RuntimeError(f"invalid antsdre200 device id: {device_id}") from exc


def _host_from_uri(uri: str) -> str | None:
    text = uri.strip()
    if not text:
        return None
    if text.startswith("ip:"):
        return text.split(":", 1)[1].strip() or None
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0].split(":", 1)[0].strip() or None


def _host_reachable(host: str | None) -> bool:
    if not host:
        return False
    ping = shutil.which("ping")
    if ping:
        result = subprocess.run(
            [ping, "-c", "1", "-W", "1", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return result.returncode == 0
    try:
        with socket.create_connection((host, ANTSDR_E200_IIO_PORT), timeout=0.75):
            return True
    except OSError:
        return False


class AntSDRE200Backend(SDRBackend):
    def list_devices(self) -> list[Device]:
        notes = (
            "Libiio-backed AD9361 control via pyadi-iio. "
            f"Configured URI: {ANTSDR_E200_URI}."
        )
        host = _host_from_uri(ANTSDR_E200_URI)
        if not _host_reachable(host):
            return []
        try:
            import adi  # type: ignore
        except Exception:
            return []

        try:
            sdr = adi.ad9361(uri=ANTSDR_E200_URI)
        except Exception:
            return []

        try:
            firmware = getattr(sdr, "firmware_version", "") or ""
            if firmware:
                notes = f"{notes} Firmware: {firmware}."
        except Exception:
            pass

        return [
            Device(
                id="antsdre200:0",
                driver="antsdre200",
                label=f"AntSDR E200 :: {ANTSDR_E200_URI}",
                serial=None,
                freq_min_hz=ANTSDR_E200_FREQ_MIN,
                freq_max_hz=ANTSDR_E200_FREQ_MAX,
                max_sample_rate_sps=ANTSDR_E200_MAX_SAMPLE_RATE,
                notes=notes,
            )
        ]

    def start_stream(self, request: StreamRequest):
        worker = Path(__file__).with_name("libiio_worker.py")
        if not worker.exists():
            raise RuntimeError(f"libiio worker not found: {worker}")

        device_index = _split_device_index(request.device_id)
        cmd = [
            sys.executable,
            str(worker),
            "--mode",
            "rx",
            "--uri",
            ANTSDR_E200_URI,
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
        raise RuntimeError("AntSDR E200 sweep backend is not implemented in sdr-gateway yet.")

    def stop_sweep(self, process) -> None:
        if process is None:
            return

    def start_tx_burst(self, request: TxBurstRequest):
        worker = Path(__file__).with_name("libiio_worker.py")
        if not worker.exists():
            raise RuntimeError(f"libiio worker not found: {worker}")

        device_index = _split_device_index(request.device_id)

        tx_path_fd, tx_path = tempfile.mkstemp(prefix="sdr_gateway_tx_", suffix=".iq")
        try:
            with os.fdopen(tx_path_fd, "wb") as f:
                f.write(request.iq_i8)
        except Exception:
            try:
                os.unlink(tx_path)
            except OSError:
                pass
            raise

        cmd = [
            sys.executable,
            str(worker),
            "--mode",
            "tx",
            "--uri",
            ANTSDR_E200_URI,
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
