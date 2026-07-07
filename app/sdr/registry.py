from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time

from app.sdr.backend import Device, SDRBackend
from app.sdr.airspy_backend import AirspyBackend
from app.sdr.antsdr_e200_backend import AntSDRE200Backend
from app.sdr.bladerf_backend import BladeRFBackend
from app.sdr.hackrf_backend import HackRFBackend
from app.sdr.mock_backend import MockBackend
from app.sdr.rtlsdr_backend import RTLSDRBackend
from app.sdr.sdrplay_backend import SDRplayBackend
from app.sdr.sidekiq_backend import SidekiqBackend

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class BackendRegistry:
    def __init__(self) -> None:
        # Keep mock backend opt-in so missing HackRF tooling is visible immediately.
        enable_mock = os.getenv("SDR_ENABLE_MOCK", "0").strip().lower() in {"1", "true", "yes"}
        self.backends: list[SDRBackend] = [
            HackRFBackend(),
            AirspyBackend(),
            AntSDRE200Backend(),
            BladeRFBackend(),
            RTLSDRBackend(),
            SDRplayBackend(),
            SidekiqBackend(),
        ]
        if enable_mock:
            self.backends.append(MockBackend())
        self._device_cache_ttl_s = max(0.0, _env_float("SDR_GATEWAY_DEVICE_CACHE_TTL_S", 3.0))
        self._device_workers = max(1, _env_int("SDR_GATEWAY_DEVICE_DISCOVERY_THREADS", min(8, len(self.backends))))
        self._device_cache: list[Device] = []
        self._device_cache_at = 0.0
        self._device_cache_lock = threading.Lock()

    def list_devices(self, *, refresh: bool = False) -> list[Device]:
        if not refresh:
            cached = self._cached_devices()
            if cached is not None:
                return cached

        devices = self._discover_devices_threaded()
        with self._device_cache_lock:
            self._device_cache = list(devices)
            self._device_cache_at = time.monotonic()
        return devices

    def backend_for_device(self, device_id: str) -> SDRBackend:
        prefix = device_id.split(":", 1)[0]
        for backend in self.backends:
            backend_name = backend.__class__.__name__.lower()
            if prefix in backend_name:
                return backend
        raise KeyError(f"No backend for device '{device_id}'")

    def invalidate_device_cache(self) -> None:
        with self._device_cache_lock:
            self._device_cache = []
            self._device_cache_at = 0.0

    def _cached_devices(self) -> list[Device] | None:
        if self._device_cache_ttl_s <= 0:
            return None
        now = time.monotonic()
        with self._device_cache_lock:
            if not self._device_cache_at or (now - self._device_cache_at) > self._device_cache_ttl_s:
                return None
            return list(self._device_cache)

    def _discover_devices_threaded(self) -> list[Device]:
        if self._device_workers <= 1 or len(self.backends) <= 1:
            return self._discover_devices_serial()

        results: list[list[Device]] = [[] for _ in self.backends]
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._device_workers, len(self.backends)),
            thread_name_prefix="sdr-device-discovery",
        ) as executor:
            futures = {
                executor.submit(self._list_backend_devices, backend): idx
                for idx, backend in enumerate(self.backends)
            }
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()

        devices: list[Device] = []
        for backend_devices in results:
            devices.extend(backend_devices)
        return devices

    def _discover_devices_serial(self) -> list[Device]:
        devices: list[Device] = []
        for backend in self.backends:
            devices.extend(self._list_backend_devices(backend))
        return devices

    @staticmethod
    def _list_backend_devices(backend: SDRBackend) -> list[Device]:
        try:
            return list(backend.list_devices())
        except Exception as exc:
            # Never let one backend crash /devices for the whole gateway.
            logger.warning("device_discovery_backend_failed backend=%s error=%s", backend.__class__.__name__, exc)
            return []
