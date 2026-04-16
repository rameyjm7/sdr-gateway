from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        method = getattr(record, "method", None)
        if method:
            payload["method"] = method
        path = getattr(record, "path", None)
        if path:
            payload["path"] = path
        status_code = getattr(record, "status_code", None)
        if status_code is not None:
            payload["status_code"] = int(status_code)
        duration_ms = getattr(record, "duration_ms", None)
        if duration_ms is not None:
            payload["duration_ms"] = int(duration_ms)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: str, json_logs: bool) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        root.addHandler(handler)
    for handler in root.handlers:
        if json_logs:
            handler.setFormatter(JsonLogFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
                    defaults={"request_id": "-"},
                )
            )


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.http_requests_total: Counter[str] = Counter()
        self.http_latency_ms_sum: Counter[str] = Counter()

    def record_request(self, method: str, path: str, status_code: int, duration_ms: int) -> None:
        key = f"{method} {path} {status_code}"
        lat_key = f"{method} {path}"
        with self._lock:
            self.http_requests_total[key] += 1
            self.http_latency_ms_sum[lat_key] += int(duration_ms)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "uptime_seconds": int(time.time() - self.started_at),
                "http_requests_total": dict(self.http_requests_total),
                "http_latency_ms_sum": dict(self.http_latency_ms_sum),
            }
