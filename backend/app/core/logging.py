"""Structured JSON logging configuration."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings

# Everything logging puts on a LogRecord itself. Anything else on a record came
# from a caller's `extra=`, which is what we want to emit.
_BUILTIN_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON for log aggregation systems."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Every `extra=` field, rather than a fixed list of them. The list this
        # replaces named five keys, so any structured field added later was
        # dropped silently -- the caller passes it, the formatter discards it,
        # and nothing reports the loss.
        for key, value in record.__dict__.items():
            if key not in _BUILTIN_RECORD_FIELDS and not key.startswith("_") and value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

    # Quieten noisy third-party loggers. Pinned to WARNING even under DEBUG:
    # httpcore emits ~15 lines per AI call (connect, TLS handshake, request,
    # response, teardown), which buried the one line that mattered when the
    # Gemini model started returning 404.
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
