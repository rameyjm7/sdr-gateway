from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
def cmd_available(command: str) -> bool:
    return shutil.which(command) is not None


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, returncode=124, stdout="", stderr="timeout")


def parse_find_output(output: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Found device"):
            if current:
                devices.append(current)
            current = {}
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip().lower()] = value.strip()
    if current:
        devices.append(current)
    return devices


def find_driver_devices(driver: str) -> list[dict[str, str]]:
    # Use external SoapySDRUtil to isolate flaky vendor modules from API process.
    util = shutil.which("SoapySDRUtil")
    if not util:
        fallback = Path("/usr/local/bin/SoapySDRUtil")
        if fallback.exists():
            util = str(fallback)
    if util:
        # Important: query one driver at a time so a broken module (e.g. sidekiq probe crash)
        # cannot break discovery for unrelated radios.
        result = run([util, f'--find=driver={driver}'])
        if result.returncode == 0:
            merged = f"{result.stdout}\n{result.stderr}"
            parsed = parse_find_output(merged)
            if parsed:
                return parsed
    return []
