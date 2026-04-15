from __future__ import annotations

import re
import subprocess


_LSUSB_LINE_RE = re.compile(
    r"^Bus\s+\d+\s+Device\s+\d+:\s+ID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.*)$"
)


def lsusb_devices() -> list[tuple[str, str]]:
    """Return [(vidpid, description)] from lsusb output."""
    try:
        proc = subprocess.run(
            ["lsusb"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    out: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        m = _LSUSB_LINE_RE.match(line.strip())
        if not m:
            continue
        vidpid = m.group(1).lower()
        desc = m.group(2).strip()
        out.append((vidpid, desc))
    return out
