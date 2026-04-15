from __future__ import annotations

import os

from app.sdr.backend import SDRBackend
from app.sdr.airspy_backend import AirspyBackend
from app.sdr.bladerf_backend import BladeRFBackend
from app.sdr.hackrf_backend import HackRFBackend
from app.sdr.mock_backend import MockBackend
from app.sdr.rtlsdr_backend import RTLSDRBackend
from app.sdr.sidekiq_backend import SidekiqBackend


class BackendRegistry:
    def __init__(self) -> None:
        # Keep mock backend opt-in so missing HackRF tooling is visible immediately.
        enable_mock = os.getenv("SDR_ENABLE_MOCK", "0").strip().lower() in {"1", "true", "yes"}
        self.backends: list[SDRBackend] = [
            HackRFBackend(),
            AirspyBackend(),
            BladeRFBackend(),
            RTLSDRBackend(),
            SidekiqBackend(),
        ]
        if enable_mock:
            self.backends.append(MockBackend())

    def list_devices(self):
        devices = []
        for backend in self.backends:
            try:
                devices.extend(backend.list_devices())
            except Exception:
                # Never let one backend crash /devices for the whole gateway.
                continue
        return devices

    def backend_for_device(self, device_id: str) -> SDRBackend:
        prefix = device_id.split(":", 1)[0]
        for backend in self.backends:
            backend_name = backend.__class__.__name__.lower()
            if prefix in backend_name:
                return backend
        raise KeyError(f"No backend for device '{device_id}'")
