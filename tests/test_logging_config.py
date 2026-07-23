"""Structured logging fields and credential redaction."""

from __future__ import annotations

import json
import logging

from pullsage.logging_config import ContextFilter, JsonFormatter


def test_json_logging_keeps_phase_metrics_and_redacts_credentials() -> None:
    record = logging.LogRecord(
        name="pullsage.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Authorization: Bearer top-secret-token",
        args=(),
        exc_info=None,
    )
    record.job_id = "job-123"
    record.event = "review_completed"
    record.codex_duration_ms = 125
    record.validation_duration_ms = 4
    record.posting_duration_ms = 0
    record.finding_count = 2
    record.posted = False
    ContextFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "Authorization: [REDACTED]"
    assert payload["job_id"] == "job-123"
    assert payload["codex_duration_ms"] == 125
    assert payload["validation_duration_ms"] == 4
    assert payload["posting_duration_ms"] == 0
    assert payload["finding_count"] == 2
    assert payload["posted"] is False


def test_json_logging_redacts_bare_tokens_and_private_keys() -> None:
    record = logging.LogRecord(
        name="pullsage.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "token github_pat_1234567890abcdefghijklmnop and "
            "ghp_1234567890abcdefghijklmnop\n"
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY-----"
        ),
        args=(),
        exc_info=None,
    )
    ContextFilter().filter(record)

    rendered = JsonFormatter().format(record)

    assert "github_pat_" not in rendered
    assert "ghp_" not in rendered
    assert "private-material" not in rendered
    assert rendered.count("[REDACTED]") >= 2
    assert "[REDACTED PRIVATE KEY]" in rendered
