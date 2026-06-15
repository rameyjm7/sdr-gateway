from __future__ import annotations

from app.services import WiFiMonitorManager


def test_prepare_monitor_interface_runs_monitor_preflight(monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr("app.services.subprocess.run", fake_run)

    WiFiMonitorManager._prepare_monitor_interface("wlan0")

    assert calls == [
        ["airmon-ng", "check", "kill"],
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "monitor"],
        ["ip", "link", "set", "wlan0", "up"],
    ]
