from __future__ import annotations

import time

from app.sdr.backend import Device
from app.sdr.registry import BackendRegistry


def _device(device_id: str, driver: str = "fake") -> Device:
    return Device(
        id=device_id,
        driver=driver,
        label=device_id,
        serial=None,
        freq_min_hz=1,
        freq_max_hz=2,
        max_sample_rate_sps=3,
    )


class _Backend:
    def __init__(self, device_id: str, delay_s: float = 0.0) -> None:
        self.device_id = device_id
        self.delay_s = delay_s
        self.calls = 0

    def list_devices(self) -> list[Device]:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        return [_device(self.device_id)]


class _FailingBackend:
    calls = 0

    def list_devices(self) -> list[Device]:
        self.calls += 1
        raise RuntimeError("probe failed")


def _registry(backends: list[object], *, ttl_s: float = 10.0, workers: int = 4) -> BackendRegistry:
    registry = BackendRegistry()
    registry.backends = backends
    registry._device_cache_ttl_s = ttl_s
    registry._device_workers = workers
    registry.invalidate_device_cache()
    return registry


def test_list_devices_preserves_backend_order_when_threaded():
    slow = _Backend("slow:0", delay_s=0.05)
    fast = _Backend("fast:0")
    registry = _registry([slow, fast])

    assert [device.id for device in registry.list_devices()] == ["slow:0", "fast:0"]


def test_list_devices_uses_cache_until_refresh_requested():
    backend = _Backend("fake:0")
    registry = _registry([backend])

    assert [device.id for device in registry.list_devices()] == ["fake:0"]
    assert [device.id for device in registry.list_devices()] == ["fake:0"]
    assert backend.calls == 1

    assert [device.id for device in registry.list_devices(refresh=True)] == ["fake:0"]
    assert backend.calls == 2


def test_list_devices_ignores_failed_backend():
    backend = _Backend("fake:0")
    registry = _registry([_FailingBackend(), backend])

    assert [device.id for device in registry.list_devices()] == ["fake:0"]
