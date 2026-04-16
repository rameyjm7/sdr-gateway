from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    log_level: str
    log_json: bool
    metrics_enabled: bool
    api_token: str

    @staticmethod
    def load() -> "Settings":
        log_level = os.getenv("SDR_GATEWAY_LOG_LEVEL", "INFO").strip().upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if log_level not in valid_levels:
            raise ValueError(
                f"Invalid SDR_GATEWAY_LOG_LEVEL '{log_level}'. Expected one of: {sorted(valid_levels)}"
            )

        api_token = os.getenv("SDR_GATEWAY_API_TOKEN", "").strip()
        # Keep token optional, but fail early on accidental tiny garbage values.
        if api_token and len(api_token) < 16:
            raise ValueError("SDR_GATEWAY_API_TOKEN is set but unexpectedly short (<16 chars).")

        return Settings(
            log_level=log_level,
            log_json=_env_bool("SDR_GATEWAY_LOG_JSON", default=False),
            metrics_enabled=_env_bool("SDR_GATEWAY_METRICS_ENABLED", default=True),
            api_token=api_token,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()
