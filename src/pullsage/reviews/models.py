"""Strict structured models shared by Codex, REST, jobs, and MCP."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from pullsage.github.models import ChangedFile, validate_repository_path

NonEmptyString = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
PositiveInteger = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInteger = Annotated[int, Field(strict=True, ge=0)]
Confidence = Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
StrictStringList = Annotated[list[NonEmptyString], Field(strict=True)]


class ReviewModel(BaseModel):
    """Base for extra-forbid structured review values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ReviewVerdict(StrEnum):
    """Overall action recommended by a validated review."""

    APPROVE = "approve"
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class RiskLevel(StrEnum):
    """Overall risk level for the supplied pull-request changes."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingSeverity(StrEnum):
    """Impact of an individual confirmed finding."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingCategory(StrEnum):
    """Review concern represented by a finding."""

    CORRECTNESS = "correctness"
    SECURITY = "security"
    RELIABILITY = "reliability"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    TESTING = "testing"


class ReviewSide(StrEnum):
    """GitHub diff side supported by PullSage's structured schema."""

    RIGHT = "RIGHT"


class ReviewFinding(ReviewModel):
    """One actionable, evidence-backed defect in changed code."""

    id: NonEmptyString
    title: NonEmptyString
    body: NonEmptyString
    severity: FindingSeverity
    category: FindingCategory
    confidence: Confidence
    file_path: NonEmptyString
    line: PositiveInteger | None = None
    start_line: PositiveInteger | None = None
    side: ReviewSide = ReviewSide.RIGHT
    suggested_fix: StrictStr | None = None
    evidence: NonEmptyString

    @field_validator("file_path")
    @classmethod
    def _validate_file_path(cls, value: str) -> str:
        return validate_repository_path(value)

    @model_validator(mode="after")
    def _validate_line_range(self) -> Self:
        if self.start_line is not None and self.line is None:
            raise ValueError("start_line requires line")
        if (
            self.start_line is not None
            and self.line is not None
            and self.start_line > self.line
        ):
            raise ValueError("start_line cannot be greater than line")
        return self


class ReviewResult(ReviewModel):
    """The complete structured result required from Codex."""

    summary: NonEmptyString
    verdict: ReviewVerdict
    confidence: Confidence
    risk_level: RiskLevel
    findings: Annotated[list[ReviewFinding], Field(strict=True)] = Field(
        default_factory=list
    )
    testing_recommendations: StrictStringList = Field(default_factory=list)
    limitations: StrictStringList = Field(default_factory=list)


class PullRequestContext(ReviewModel):
    """Bounded, sanitized pull-request input passed to the Codex runner."""

    repository_full_name: NonEmptyString
    pull_request_number: PositiveInteger
    title: NonEmptyString
    author_login: NonEmptyString
    base_branch: NonEmptyString
    head_branch: NonEmptyString
    head_sha: NonEmptyString
    body: StrictStr | None = None
    state: NonEmptyString
    additions: NonNegativeInteger
    deletions: NonNegativeInteger
    changed_files: Annotated[list[ChangedFile], Field(strict=True)]
    unified_diff: StrictStr
    diff_original_length: NonNegativeInteger
    diff_truncated: StrictBool = False
    warnings: StrictStringList = Field(default_factory=list)


# Concise aliases retained as part of the public domain API.
Verdict = ReviewVerdict
Severity = FindingSeverity
Category = FindingCategory

