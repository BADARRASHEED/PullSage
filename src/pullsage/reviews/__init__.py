"""Shared structured review domain and orchestration services."""

from pullsage.reviews.formatter import (
    build_inline_comments,
    format_review,
    format_review_markdown,
    review_event_for_result,
)
from pullsage.reviews.models import (
    Category,
    FindingCategory,
    FindingSeverity,
    PullRequestContext,
    ReviewFinding,
    ReviewResult,
    ReviewSide,
    ReviewVerdict,
    RiskLevel,
    Severity,
    Verdict,
)
from pullsage.reviews.service import CodexRunnerProtocol, ReviewService
from pullsage.reviews.validation import (
    coerce_review_result,
    deduplicate_findings,
    extract_changed_lines,
    validate_and_filter_review,
    validate_review,
)

__all__ = [
    "Category",
    "CodexRunnerProtocol",
    "FindingCategory",
    "FindingSeverity",
    "PullRequestContext",
    "ReviewFinding",
    "ReviewResult",
    "ReviewService",
    "ReviewSide",
    "ReviewVerdict",
    "RiskLevel",
    "Severity",
    "Verdict",
    "build_inline_comments",
    "coerce_review_result",
    "deduplicate_findings",
    "extract_changed_lines",
    "format_review",
    "format_review_markdown",
    "review_event_for_result",
    "validate_and_filter_review",
    "validate_review",
]
