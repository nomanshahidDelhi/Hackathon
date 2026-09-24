"""Logging that cannot leak: every record is redacted after formatting, so
secrets in messages, arguments and exception tracebacks are all scrubbed."""
from __future__ import annotations

import logging
import os

from .redact import redact


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record)) or ""


def setup_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    # The HTTP client logs full request URLs at INFO; keep it quiet unless debugging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
