"""Models used by PullSage's in-memory review job system."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from pullsage.reviews.models import ReviewResult


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class JobStatus(str, Enum):
    """Lifecycle states for an asynchronous pull-request review."""

    QUEUED = "queued"
    FETCHING_CONTEXT = "fetching_context"
    REVIEWING = "reviewing"
    VALIDATING = "validating"
    POSTING = "posting"
    COMPLETED = "completed"
    FAILED = "failed"


class JobSource(str, Enum):
    """Entry point which created a review job."""

    MANUAL = "manual"
    WEBHOOK = "webhook"
    MCP = "mcp"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})


class ReviewJob(BaseModel):
    """A single ephemeral pull-request review job."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    job_id: UUID = Field(default_factory=uuid4)
    owner: str = Field(min_length=1, max_length=255)
    repository: str = Field(min_length=1, max_length=255)
    pull_request_number: int = Field(ge=1)
    source: JobSource
    post_comments: bool = False
    head_sha: str | None = Field(default=None, min_length=1, max_length=128)
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    result: ReviewResult | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether no further state changes are expected."""

        return self.status in TERMINAL_JOB_STATUSES

    @property
    def deduplication_key(self) -> tuple[str, str, int, str] | None:
        """Return the active-job key when a head SHA is known."""

        if not self.head_sha:
            return None
        return (
            self.owner.casefold(),
            self.repository.casefold(),
            self.pull_request_number,
            self.head_sha.casefold(),
        )


class JobSubmission(BaseModel):
    """Result returned internally when a job is queued or deduplicated."""

    model_config = ConfigDict(extra="forbid")

    job: ReviewJob
    created: bool
