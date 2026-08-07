"""Structured logging. Never use bare `print` anywhere in the project.

`configure_logging` sets up the root `nomad` logger once at boot.
`get_logger` returns a logger (optionally with bound contextual fields, e.g.
`session_id`, `turn_id`) for use in a module.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal

_RESERVED = frozenset(logging.makeLogRecord({}).__dict__.keys()) | {"message", "asctime"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value for key, value in record.__dict__.items() if key not in _RESERVED
        }
        if extras:
            extra_str = " ".join(f"{k}={v!r}" for k, v in extras.items())
            base = f"{base} [{extra_str}]"
        return base


def configure_logging(level: str = "INFO", fmt: Literal["console", "json"] = "console") -> None:
    """Configure the root `nomad` logger. Safe to call more than once."""
    logger = logging.getLogger("nomad")
    logger.setLevel(level.upper())
    logger.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _ConsoleFormatter())
    logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a logger under the `nomad` namespace, optionally bound with context fields."""
    base = logging.getLogger(name if name.startswith("nomad") else f"nomad.{name}")
    return logging.LoggerAdapter(base, extra=context)
