"""Logging setup: one simple, structured line per record.

Format: `<timestamp> <LEVEL> <logger> [rid=<request id>] <message>`. Messages use `key=value` pairs so they are
easy to grep. Every HTTP request gets an id (X-Request-ID header, echoed or generated) that is attached to
every log line written while handling it, so one request can be followed through the logs.
Never pass secrets (API keys, passwords, full DB URLs) into log calls.
"""

from __future__ import annotations

import contextvars
import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s rid=%(request_id)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Adds `request_id` to every record (the current request's id, or "-" outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger once, idempotently."""
    root = logging.getLogger()
    root.setLevel(level)

    # Avoid stacking duplicate handlers if called more than once (e.g. reloads, tests).
    if not any(getattr(h, "_agentflow", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        handler.addFilter(RequestIdFilter())
        handler._agentflow = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    # SQLAlchemy's engine logger is noisy at INFO; only show it when debugging.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if level == "DEBUG" else logging.WARNING
    )
