"""Concurrency-safe in-memory storage for review jobs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pullsage.exceptions import JobNotFoundError
from pullsage.jobs.models import (
    TERMINAL_JOB_STATUSES,
    JobSource,
    JobStatus,
    ReviewJob,
)
from pullsage.reviews.models import ReviewResult


class InvalidJobTransitionError(ValueError):
    """Raised when code attempts an invalid job lifecycle transition."""


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.FETCHING_CONTEXT, JobStatus.FAILED}),
    JobStatus.FETCHING_CONTEXT: frozenset(
        {JobStatus.REVIEWING, JobStatus.FAILED}
    ),
    JobStatus.REVIEWING: frozenset(
        {
            JobStatus.VALIDATING,
            JobStatus.POSTING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.VALIDATING: frozenset(
        {JobStatus.POSTING, JobStatus.COMPLETED, JobStatus.FAILED}
    ),
    JobStatus.POSTING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


def _coerce_job_id(job_id: UUID | str) -> UUID | None:
    if isinstance(job_id, UUID):
        return job_id
    try:
        return UUID(str(job_id))
    except (TypeError, ValueError, AttributeError):
        return None


class InMemoryJobStore:
    """Store jobs for the lifetime of one PullSage process.

    The store deliberately has no persistence. All data is lost when the
    process exits, and completed jobs are removed after the configured
    retention period.
    """

    def __init__(self, retention_seconds: float = 3600) -> None:
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be non-negative")
        self._retention_seconds = float(retention_seconds)
        self._jobs: dict[UUID, ReviewJob] = {}
        self._active_by_key: dict[tuple[str, str, int, str], UUID] = {}
        self._lock = asyncio.Lock()

    @property
    def retention_seconds(self) -> float:
        return self._retention_seconds

    def __len__(self) -> int:
        """Return the current retained-job count."""

        return len(self._jobs)

    async def get_or_create_job(
        self,
        *,
        owner: str,
        repository: str,
        pull_request_number: int,
        source: JobSource | str,
        post_comments: bool = False,
        head_sha: str | None = None,
    ) -> tuple[ReviewJob, bool]:
        """Atomically create a job or return an active matching head-SHA job."""

        candidate = ReviewJob(
            owner=owner,
            repository=repository,
            pull_request_number=pull_request_number,
            source=source,
            post_comments=post_comments,
            head_sha=head_sha,
        )
        async with self._lock:
            key = candidate.deduplication_key
            if key is not None:
                existing_id = self._active_by_key.get(key)
                existing = self._jobs.get(existing_id) if existing_id else None
                if existing is not None and not existing.is_terminal:
                    return existing.model_copy(deep=True), False
                self._active_by_key.pop(key, None)

            self._jobs[candidate.job_id] = candidate
            if key is not None:
                self._active_by_key[key] = candidate.job_id
            return candidate.model_copy(deep=True), True

    async def create_job(
        self,
        *,
        owner: str,
        repository: str,
        pull_request_number: int,
        source: JobSource | str,
        post_comments: bool = False,
        head_sha: str | None = None,
    ) -> ReviewJob:
        """Create a job, returning an existing active duplicate when relevant."""

        job, _created = await self.get_or_create_job(
            owner=owner,
            repository=repository,
            pull_request_number=pull_request_number,
            source=source,
            post_comments=post_comments,
            head_sha=head_sha,
        )
        return job

    async def add(self, job: ReviewJob) -> ReviewJob:
        """Add an already-validated job to the store."""

        async with self._lock:
            if job.job_id in self._jobs:
                raise ValueError(f"Job {job.job_id} already exists")

            key = job.deduplication_key
            if key is not None:
                existing_id = self._active_by_key.get(key)
                existing = self._jobs.get(existing_id) if existing_id else None
                if existing is not None and not existing.is_terminal:
                    return existing.model_copy(deep=True)

            stored = job.model_copy(deep=True)
            self._jobs[stored.job_id] = stored
            if key is not None and not stored.is_terminal:
                self._active_by_key[key] = stored.job_id
            return stored.model_copy(deep=True)

    async def get(self, job_id: UUID | str) -> ReviewJob | None:
        """Return a defensive copy of a job, or ``None`` if it is absent."""

        normalized = _coerce_job_id(job_id)
        if normalized is None:
            return None
        async with self._lock:
            job = self._jobs.get(normalized)
            return job.model_copy(deep=True) if job is not None else None

    async def require(self, job_id: UUID | str) -> ReviewJob:
        """Return a job or raise :class:`JobNotFoundError`."""

        job = await self.get(job_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        return job

    async def transition(
        self,
        job_id: UUID | str,
        status: JobStatus | str,
        *,
        result: ReviewResult | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> ReviewJob:
        """Move a job to a valid next state and update lifecycle timestamps."""

        normalized = _coerce_job_id(job_id)
        if normalized is None:
            raise JobNotFoundError(str(job_id))
        next_status = JobStatus(status)
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        async with self._lock:
            job = self._jobs.get(normalized)
            if job is None:
                raise JobNotFoundError(str(job_id))

            if next_status != job.status:
                allowed = _ALLOWED_TRANSITIONS[job.status]
                if next_status not in allowed:
                    raise InvalidJobTransitionError(
                        f"Cannot move job from {job.status.value} "
                        f"to {next_status.value}"
                    )

            if next_status not in {JobStatus.QUEUED, JobStatus.FAILED}:
                job.started_at = job.started_at or timestamp
            elif next_status == JobStatus.FAILED and job.status != JobStatus.QUEUED:
                job.started_at = job.started_at or timestamp

            job.status = next_status
            if result is not None:
                job.result = result
            if next_status == JobStatus.FAILED:
                job.error = error or "Review job failed"
                job.completed_at = timestamp
            elif next_status == JobStatus.COMPLETED:
                job.error = None
                job.completed_at = timestamp
            elif error is not None:
                job.error = error

            if next_status in TERMINAL_JOB_STATUSES:
                key = job.deduplication_key
                if key is not None and self._active_by_key.get(key) == job.job_id:
                    self._active_by_key.pop(key, None)

            return job.model_copy(deep=True)

    async def update_status(
        self,
        job_id: UUID | str,
        status: JobStatus | str,
        **kwargs: object,
    ) -> ReviewJob:
        """Compatibility alias for :meth:`transition`."""

        result = kwargs.pop("result", None)
        error = kwargs.pop("error", None)
        now = kwargs.pop("now", None)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected update fields: {unexpected}")
        return await self.transition(
            job_id,
            status,
            result=result,  # type: ignore[arg-type]
            error=error if isinstance(error, str) or error is None else str(error),
            now=now if isinstance(now, datetime) or now is None else None,
        )

    async def fail(self, job_id: UUID | str, error: str) -> ReviewJob:
        """Mark a non-terminal job as failed."""

        return await self.transition(job_id, JobStatus.FAILED, error=error)

    async def list_jobs(self) -> list[ReviewJob]:
        """Return all retained jobs, oldest first."""

        async with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at)
            return [job.model_copy(deep=True) for job in jobs]

    async def cleanup_expired(
        self,
        *,
        now: datetime | None = None,
        retention_seconds: float | None = None,
    ) -> int:
        """Remove terminal jobs older than the retention window."""

        effective_retention = (
            self._retention_seconds
            if retention_seconds is None
            else float(retention_seconds)
        )
        if effective_retention < 0:
            raise ValueError("retention_seconds must be non-negative")
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        cutoff = timestamp - timedelta(seconds=effective_retention)

        async with self._lock:
            expired_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.is_terminal
                and job.completed_at is not None
                and job.completed_at <= cutoff
            ]
            for job_id in expired_ids:
                job = self._jobs.pop(job_id)
                key = job.deduplication_key
                if key is not None and self._active_by_key.get(key) == job_id:
                    self._active_by_key.pop(key, None)
            return len(expired_ids)

    async def cleanup(self, **kwargs: object) -> int:
        """Compatibility alias for :meth:`cleanup_expired`."""

        now = kwargs.pop("now", None)
        retention_seconds = kwargs.pop("retention_seconds", None)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected cleanup arguments: {unexpected}")
        return await self.cleanup_expired(
            now=now if isinstance(now, datetime) or now is None else None,
            retention_seconds=(
                float(retention_seconds)
                if retention_seconds is not None
                else None
            ),
        )

    async def delete(self, job_id: UUID | str) -> bool:
        """Delete one job if present."""

        normalized = _coerce_job_id(job_id)
        if normalized is None:
            return False
        async with self._lock:
            job = self._jobs.pop(normalized, None)
            if job is None:
                return False
            key = job.deduplication_key
            if key is not None and self._active_by_key.get(key) == normalized:
                self._active_by_key.pop(key, None)
            return True

    async def count(self) -> int:
        """Return the retained job count while holding the store lock."""

        async with self._lock:
            return len(self._jobs)
