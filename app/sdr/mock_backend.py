from __future__ import annotations

import os
import subprocess

from app.sdr.backend import Device, SDRBackend, StreamRequest, SweepRequest


class MockBackend(SDRBackend):
    def list_devices(self) -> list[Device]:
        return [
            Device(
                id="mock:0",
                driver="mock",
                label="Mock SDR",
                serial="MOCK0001",
                freq_min_hz=100_000,
                freq_max_hz=6_000_000_000,
                max_sample_rate_sps=2_000_000,
                notes="Synthetic IQ source for local development.",
            )
        ]

    def start_stream(self, request: StreamRequest):
        # Generates random int8 IQ bytes endlessly (I,Q,I,Q...).
        return subprocess.Popen(
            ["bash", "-lc", "while true; do head -c 16384 /dev/urandom; done"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=os.environ.copy(),
        )

    def stop_stream(self, process) -> None:
        if process.poll() is None:
            process.terminate()

    def start_sweep(self, request: SweepRequest):
        # Emit CSV lines close to hackrf_sweep output shape.
        line = "2026-01-01,00:00:00,2400000000,2401000000,100000,1,-80.0,-79.5,-79.2\\n"
        return subprocess.Popen(
            ["bash", "-lc", f"while true; do printf '{line}'; sleep 0.2; done"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )

    def stop_sweep(self, process) -> None:
        if process.poll() is None:
            process.terminate()
