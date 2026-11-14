"""Runtime configuration, read from the environment."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from err


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Everything the service needs to run."""

    token: str
    base_url: str
    state_interval_sec: int
    event_interval_sec: int
    metrics_host: str
    metrics_port: int
    pg_dsn: str | None
    page_size: int
    max_pages: int
    backfill: bool
    state_samples: bool
    request_timeout_sec: int
    log_level: int

    @property
    def events_enabled(self) -> bool:
        """Trip/refuel ingestion needs Postgres; without a DSN we run metrics only."""
        return self.pg_dsn is not None


def load_config() -> Config:
    """Build a Config from environment variables."""
    token = os.environ.get("AXIMOTE_TOKEN", "").strip()
    if not token:
        raise ConfigError("AXIMOTE_TOKEN is required")

    dsn = os.environ.get("AXIMOTE_PG_DSN", "").strip() or None

    level_name = os.environ.get("AXIMOTE_LOG_LEVEL", "INFO").strip().upper()
    log_level = getattr(logging, level_name, logging.INFO)

    return Config(
        token=token,
        base_url=os.environ.get("AXIMOTE_BASE_URL", "https://api.aximote.com").rstrip(
            "/"
        ),
        state_interval_sec=_int("AXIMOTE_STATE_INTERVAL", 180),
        event_interval_sec=_int("AXIMOTE_EVENT_INTERVAL", 300),
        metrics_host=os.environ.get("AXIMOTE_METRICS_HOST", "0.0.0.0"),
        metrics_port=_int("AXIMOTE_METRICS_PORT", 9684),
        pg_dsn=dsn,
        page_size=_int("AXIMOTE_PAGE_SIZE", 100),
        max_pages=_int("AXIMOTE_MAX_PAGES", 200),
        backfill=_bool("AXIMOTE_BACKFILL", True),
        state_samples=_bool("AXIMOTE_STATE_SAMPLES", True),
        request_timeout_sec=_int("AXIMOTE_REQUEST_TIMEOUT", 30),
        log_level=log_level,
    )
