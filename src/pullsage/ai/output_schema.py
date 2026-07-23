"""Pydantic-backed JSON schema and parsing for Codex output."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from pullsage.reviews.models import ReviewResult


def review_json_schema() -> dict[str, Any]:
    """Return the strict schema passed to ``codex exec --output-schema``."""

    return ReviewResult.model_json_schema(mode="validation")


def parse_review_output(raw_output: str) -> ReviewResult:
    """Parse JSON and validate it against the review domain model."""

    value = json.loads(raw_output)
    return ReviewResult.model_validate(value)


def format_validation_error(error: Exception) -> str:
    """Format bounded validation details without echoing untrusted model values."""

    if isinstance(error, ValidationError):
        details = error.errors(include_url=False, include_context=False, include_input=False)
        return json.dumps(details, ensure_ascii=False, default=str)[:8_000]
    if isinstance(error, json.JSONDecodeError):
        return (
            f"Invalid JSON at line {error.lineno}, column {error.colno}: "
            f"{error.msg}"
        )
    return f"{type(error).__name__}: output did not match the review schema"
