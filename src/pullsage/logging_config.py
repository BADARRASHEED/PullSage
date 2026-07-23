"""Structured standard-library logging with request and job correlation."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_request_id: ContextVar[str] = ContextVar("pullsage_request_id", default="-")
_job_id: ContextVar[str] = ContextVar("pullsage_job_id", default="-")

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)((?:github_)?token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)((?:webhook_)?secret\s*[:=]\s*)[^\s,;]+"),
)


def _redact(value: str) -> str:
    """Redact common credential forms from diagnostic messages."""

    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


class ContextFilter(logging.Filter):
    """Attach correlation identifiers to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        record.job_id = _job_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log event for local and hosted runtimes."""

    _optional_fields = (
        "event",
        "repository",
        "pull_request_number",
        "duration_ms",
        "status",
        "delivery_id",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
            "request_id": getattr(record, "request_id", "-"),
            "job_id": getattr(record, "job_id", "-"),
        }
        for field in self._optional_fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once with safe structured output."""

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def set_request_id(value: str) -> Token[str]:
    """Bind a request identifier and return a reset token."""

    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    """Restore the prior request identifier."""

    _request_id.reset(token)


def set_job_id(value: str) -> Token[str]:
    """Bind a job identifier and return a reset token."""

    return _job_id.set(value)


def reset_job_id(token: Token[str]) -> None:
    """Restore the prior job identifier."""

    _job_id.reset(token)
