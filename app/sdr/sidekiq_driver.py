from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess


logger = logging.getLogger("sdr_gateway.sidekiq")


def ensure_sidekiq_driver_loaded() -> None:
    if os.getenv("SDR_GATEWAY_LOAD_SIDEKIQ_DRIVER", "1").strip().lower() in {"0", "false", "no", "off"}:
        logger.info("sidekiq_driver_load_skipped disabled_by_env=1")
        return

    base_dir = Path(os.getenv("SIDEKIQ_HOME", "/home/jake/sidekiq")).resolve()
    driver_dir = Path(os.getenv("SIDEKIQ_DRIVER_DIR", str(base_dir / "sidekiq_image_current" / "driver"))).resolve()
    load_script = Path(os.getenv("SIDEKIQ_DRIVER_LOAD_SCRIPT", str(driver_dir / "load_sidekiq_drivers.sh"))).resolve()
    pci_addr = os.getenv("SIDEKIQ_PCI_ADDR", "06:00.0").strip()

    if not load_script.exists():
        logger.info("sidekiq_driver_load_unavailable script=%s", load_script)
        return

    try:
        result = subprocess.run(
            [str(load_script), str(driver_dir)],
            cwd=str(base_dir),
            text=True,
            capture_output=True,
            timeout=float(os.getenv("SIDEKIQ_DRIVER_LOAD_TIMEOUT_S", "30")),
            check=False,
        )
        if result.returncode == 0:
            logger.info("sidekiq_driver_load_ok script=%s", load_script)
        else:
            logger.warning(
                "sidekiq_driver_load_failed script=%s returncode=%s stderr=%s",
                load_script,
                result.returncode,
                result.stderr[-1000:].strip(),
            )
    except Exception as exc:
        logger.warning("sidekiq_driver_load_error script=%s error=%s", load_script, exc)
        return

    if not pci_addr:
        return
    try:
        pci = subprocess.run(
            ["lspci", "-vv", "-s", pci_addr],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if pci.returncode == 0:
            logger.info("sidekiq_pci_status addr=%s output=%s", pci_addr, pci.stdout[:2000].strip())
        else:
            logger.warning("sidekiq_pci_status_failed addr=%s stderr=%s", pci_addr, pci.stderr[-1000:].strip())
    except Exception as exc:
        logger.warning("sidekiq_pci_status_error addr=%s error=%s", pci_addr, exc)
