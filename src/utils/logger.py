"""Structured logging helpers shared by every Kollektiv component.

A single :func:`configure_logging` call (performed by the API entry point and
by :mod:`src.orchestrator.app`) gives every module a consistent, colourised
console logger with an optional JSON mode for shipping logs to aggregators.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any, Dict, Optional

_CONFIGURED = False

#: Third party loggers that are noisy at DEBUG level.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore2",
    "uvicorn.access",
    "apscheduler.executors.default",
    "openai",
    "mcp",
)

_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
    "RESET": "\033[0m",
}


class JsonFormatter(logging.Formatter):
    """Render log records as single line JSON objects.

    Enabled with the ``KOLLEKTIV_LOG_JSON=1`` environment variable, which is
    convenient when running inside Docker or a log aggregator.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record`` to a JSON string."""
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


class ColorFormatter(logging.Formatter):
    """Human friendly console formatter with optional ANSI colours."""

    def __init__(self, use_color: bool = True) -> None:
        super().__init__("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record``, colourising the level name when enabled."""
        rendered = super().format(record)
        if not self.use_color:
            return rendered
        color = _COLORS.get(record.levelname, "")
        reset = _COLORS["RESET"] if color else ""
        return rendered.replace(record.levelname, f"{color}{record.levelname}{reset}", 1)


def configure_logging(level: Optional[str] = None, force: bool = False) -> None:
    """Configure the root logger exactly once per process.

    Args:
        level: Log level name; falls back to ``LOG_LEVEL`` then ``INFO``.
        force: Reconfigure even if logging was already set up.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    resolved = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    use_json = os.getenv("KOLLEKTIV_LOG_JSON", "").strip().lower() in {"1", "true", "yes", "on"}
    use_color = sys.stderr.isatty() and not use_json

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if use_json else ColorFormatter(use_color))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, resolved, logging.INFO))

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a logger adapter carrying static contextual fields.

    Args:
        name: Logger name, normally ``__name__``.
        **context: Extra fields merged into every record (e.g. ``agent_id``).

    Returns:
        A :class:`logging.LoggerAdapter` whose records carry ``extra_fields``.
    """
    configure_logging()
    logger = logging.getLogger(name)
    if not context:
        context = {}
    return logging.LoggerAdapter(logger, {"context": context})


class _ContextAdapter(logging.LoggerAdapter):
    """LoggerAdapter that keeps ``extra_fields`` structured for JsonFormatter."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = kwargs.setdefault("extra", {})
        context = self.extra or {}
        extra["extra_fields"] = {key: value for key, value in context.items() if key != "context"}
        nested = context.get("context") or {}
        if isinstance(nested, dict):
            extra["extra_fields"].update(nested)
        return msg, kwargs


def logger_with_context(name: str, **context: Any) -> logging.LoggerAdapter:
    """Like :func:`get_logger` but emits context as structured log fields."""
    configure_logging()
    return _ContextAdapter(logging.getLogger(name), dict(context))


__all__ = [
    "configure_logging",
    "get_logger",
    "logger_with_context",
    "JsonFormatter",
    "ColorFormatter",
]
