"""Focused tests for the process-local job lifecycle."""

from datetime import UTC, datetime, timedelta

import pytest

from pullsage.jobs import (
    InMemoryJobStore,
    InvalidJobTransitionError,
    JobSource,
    JobStatus,
)


@pytest.mark.asyncio
async def test_job_state_transitions_set_lifecycle_timestamps() -> None:
    store = InMemoryJobStore()
    job = await store.create_job(
        owner="octo-org",
        repository="example",
        pull_request_number=42,
        source=JobSource.MANUAL,
    )

    fetching = await store.transition(job.job_id, JobStatus.FETCHING_CONTEXT)
    reviewing = await store.transition(job.job_id, JobStatus.REVIEWING)
    validating = await store.transition(job.job_id, JobStatus.VALIDATING)
    completed = await store.transition(job.job_id, JobStatus.COMPLETED)

    assert fetching.started_at is not None
    assert reviewing.started_at == fetching.started_at
    assert validating.status is JobStatus.VALIDATING
    assert completed.status is JobStatus.COMPLETED
    assert completed.completed_at is not None
    assert completed.error is None


@pytest.mark.asyncio
async def test_invalid_job_transition_is_rejected() -> None:
    store = InMemoryJobStore()
    job = await store.create_job(
        owner="octo-org",
        repository="example",
        pull_request_number=7,
        source=JobSource.WEBHOOK,
    )

    with pytest.raises(InvalidJobTransitionError):
        await store.transition(job.job_id, JobStatus.COMPLETED)


@pytest.mark.asyncio
async def test_active_jobs_are_deduplicated_by_pull_request_head() -> None:
    store = InMemoryJobStore()
    first, first_created = await store.get_or_create_job(
        owner="Octo-Org",
        repository="Example",
        pull_request_number=8,
        source=JobSource.WEBHOOK,
        head_sha="abc123def",
    )
    duplicate, duplicate_created = await store.get_or_create_job(
        owner="octo-org",
        repository="example",
        pull_request_number=8,
        source=JobSource.WEBHOOK,
        head_sha="ABC123DEF",
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.job_id == first.job_id


@pytest.mark.asyncio
async def test_expired_terminal_jobs_are_removed() -> None:
    store = InMemoryJobStore(retention_seconds=10)
    job = await store.create_job(
        owner="octo-org",
        repository="example",
        pull_request_number=9,
        source=JobSource.MANUAL,
    )
    completed_at = datetime(2026, 1, 1, tzinfo=UTC)
    await store.transition(
        job.job_id,
        JobStatus.FETCHING_CONTEXT,
        now=completed_at - timedelta(seconds=3),
    )
    await store.transition(
        job.job_id,
        JobStatus.REVIEWING,
        now=completed_at - timedelta(seconds=2),
    )
    await store.transition(
        job.job_id,
        JobStatus.VALIDATING,
        now=completed_at - timedelta(seconds=1),
    )
    await store.transition(
        job.job_id,
        JobStatus.COMPLETED,
        now=completed_at,
    )

    assert (
        await store.cleanup_expired(
            now=completed_at + timedelta(seconds=9)
        )
        == 0
    )
    assert (
        await store.cleanup_expired(
            now=completed_at + timedelta(seconds=10)
        )
        == 1
    )
    assert await store.get(job.job_id) is None
