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
_BARE_TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"sk-[A-Za-z0-9_-]{16,})\b"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    flags=re.IGNORECASE | re.DOTALL,
)


def _redact(value: str) -> str:
    """Redact common credential forms from diagnostic messages."""

    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    redacted = _BARE_TOKEN_PATTERN.sub("[REDACTED]", redacted)
    redacted = _PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", redacted)
    return redacted


class ContextFilter(logging.Filter):
    """Attach correlation identifiers to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        request_id = _request_id.get()
        job_id = _job_id.get()
        if request_id != "-" or not hasattr(record, "request_id"):
            record.request_id = request_id
        if job_id != "-" or not hasattr(record, "job_id"):
            record.job_id = job_id
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
        "codex_duration_ms",
        "validation_duration_ms",
        "posting_duration_ms",
        "finding_count",
        "posted",
        "changed_file_count",
        "diff_truncated",
        "worker_count",
        "worker_index",
        "source",
        "removed_count",
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
