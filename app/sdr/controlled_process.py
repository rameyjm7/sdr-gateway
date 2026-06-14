from __future__ import annotations

import json
import subprocess
import threading
from typing import Any

from app.sdr.backend import StreamRequest


class ControlledStreamProcess:
    """Small Popen wrapper with a line-oriented retune control channel."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._control_lock = threading.Lock()

    @property
    def stdout(self) -> Any | None:
        return self._process.stdout

    @property
    def stderr(self) -> Any | None:
        return self._process.stderr

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def kill(self) -> None:
        self._process.kill()

    def retune(self, request: StreamRequest) -> bool:
        if self._process.poll() is not None or self._process.stdin is None:
            return False
        command = {
            "op": "retune",
            "center_freq_hz": int(request.center_freq_hz),
            "sample_rate_sps": int(request.sample_rate_sps),
            "baseband_filter_hz": int(request.baseband_filter_hz or 0),
            "lna_gain_db": int(request.lna_gain_db),
            "vga_gain_db": int(request.vga_gain_db),
            "amp_enable": bool(request.amp_enable),
        }
        payload = (json.dumps(command, separators=(",", ":")) + "\n").encode("ascii")
        with self._control_lock:
            try:
                self._process.stdin.write(payload)
                self._process.stdin.flush()
                return True
            except (BrokenPipeError, OSError):
                return False
