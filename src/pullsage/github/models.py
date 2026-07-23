"""Strict domain models for the subset of GitHub data PullSage uses."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self

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

NonEmptyString = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
PositiveInteger = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInteger = Annotated[int, Field(strict=True, ge=0)]


def validate_repository_path(value: str) -> str:
    """Validate a repository-relative POSIX path without normalizing it."""

    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise ValueError("file_path must be a repository-relative POSIX path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("file_path contains an unsafe path segment")
    return value


class GitHubModel(BaseModel):
    """Base for immutable, extra-forbid GitHub domain values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_default=True,
    )


class PullRequestState(StrEnum):
    """GitHub pull-request states used by the REST API."""

    OPEN = "open"
    CLOSED = "closed"


class ChangedFileStatus(StrEnum):
    """Statuses currently returned by GitHub's changed-files endpoint."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class ReviewEvent(StrEnum):
    """Review actions accepted by GitHub's create-review endpoint."""

    COMMENT = "COMMENT"
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class ReviewCommentSide(StrEnum):
    """GitHub diff side supported for PullSage inline findings."""

    RIGHT = "RIGHT"


class PullRequest(GitHubModel):
    """Sanitized pull-request metadata required by the review pipeline."""

    repository_full_name: NonEmptyString
    number: PositiveInteger
    title: NonEmptyString
    body: StrictStr | None = None
    state: PullRequestState
    draft: StrictBool
    html_url: NonEmptyString
    author_login: NonEmptyString
    base_ref: NonEmptyString
    head_ref: NonEmptyString
    head_sha: NonEmptyString
    additions: NonNegativeInteger
    deletions: NonNegativeInteger
    changed_files: NonNegativeInteger
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChangedFile(GitHubModel):
    """A changed file and the optional bounded patch supplied by GitHub."""

    filename: NonEmptyString
    status: ChangedFileStatus
    additions: NonNegativeInteger
    deletions: NonNegativeInteger
    changes: NonNegativeInteger
    sha: NonEmptyString | None = None
    previous_filename: NonEmptyString | None = None
    blob_url: NonEmptyString | None = None
    raw_url: NonEmptyString | None = None
    contents_url: NonEmptyString | None = None
    patch: StrictStr | None = None

    @field_validator("filename", "previous_filename")
    @classmethod
    def _validate_paths(cls, value: str | None) -> str | None:
        return None if value is None else validate_repository_path(value)


class PullRequestDiff(GitHubModel):
    """A unified diff bounded to the configured character budget."""

    content: StrictStr
    original_length: NonNegativeInteger
    truncated: StrictBool
    max_chars: PositiveInteger | None = None

    @model_validator(mode="after")
    def _validate_lengths(self) -> Self:
        content_length = len(self.content)
        if self.original_length < content_length:
            raise ValueError("original_length cannot be shorter than content")
        if self.truncated and self.original_length <= content_length:
            raise ValueError("a truncated diff must omit at least one character")
        if not self.truncated and self.original_length != content_length:
            raise ValueError("an untruncated diff must report its exact length")
        if self.max_chars is not None and content_length > self.max_chars:
            raise ValueError("content exceeds max_chars")
        return self


class GitHubReviewComment(GitHubModel):
    """An inline comment safe to include in one GitHub review submission."""

    path: NonEmptyString
    body: NonEmptyString
    line: PositiveInteger
    side: ReviewCommentSide = ReviewCommentSide.RIGHT
    start_line: PositiveInteger | None = None
    start_side: ReviewCommentSide | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_repository_path(value)

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        if self.start_line is not None:
            if self.start_line > self.line:
                raise ValueError("start_line cannot be greater than line")
            if self.start_side is None:
                raise ValueError("start_side is required with start_line")
        elif self.start_side is not None:
            raise ValueError("start_side requires start_line")
        return self

    def to_api_payload(self) -> dict[str, str | int]:
        """Return only keys understood by GitHub's review API."""

        payload: dict[str, str | int] = {
            "path": self.path,
            "body": self.body,
            "line": self.line,
            "side": self.side.value,
        }
        if self.start_line is not None:
            payload["start_line"] = self.start_line
            payload["start_side"] = (self.start_side or ReviewCommentSide.RIGHT).value
        return payload


class PostedReview(GitHubModel):
    """Sanitized response from GitHub after a review is created."""

    id: PositiveInteger
    state: NonEmptyString
    html_url: NonEmptyString | None = None
    body: StrictStr | None = None
    submitted_at: datetime | None = None


class PullRequestWebhook(GitHubModel):
    """Minimal, sanitized data extracted from a pull-request webhook."""

    action: NonEmptyString
    owner: NonEmptyString
    repository: NonEmptyString
    repository_full_name: NonEmptyString
    pull_request_number: PositiveInteger
    draft: StrictBool
    head_sha: NonEmptyString

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Extract required values without retaining the untrusted raw payload."""

        try:
            pull_request = payload["pull_request"]
            repository = payload["repository"]
            owner = repository["owner"]
            return cls(
                action=payload["action"],
                owner=owner["login"],
                repository=repository["name"],
                repository_full_name=repository["full_name"],
                pull_request_number=pull_request["number"],
                draft=pull_request["draft"],
                head_sha=pull_request["head"]["sha"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("GitHub pull_request payload is missing required fields") from exc


# Public compatibility names that describe the endpoint payload explicitly.
PullRequestMetadata = PullRequest
PullRequestFile = ChangedFile
