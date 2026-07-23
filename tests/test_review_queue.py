"""Queue admission, shutdown, and worker-failure integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from pullsage.exceptions import CodexRuntimeError, WorkerShutdownError
from pullsage.jobs.models import JobSource, JobStatus
from pullsage.jobs.store import InMemoryJobStore
from pullsage.jobs.worker import ReviewQueue
from pullsage.reviews.models import ReviewResult


def _review() -> ReviewResult:
    return ReviewResult.model_validate(
        {
            "summary": "No supported defects were found.",
            "verdict": "comment",
            "confidence": 0.9,
            "risk_level": "low",
            "findings": [],
            "testing_recommendations": [],
            "limitations": ["Tests were not executed."],
        }
    )


class _BlockingReviewService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def review_pull_request(
        self,
        _owner: str,
        _repository: str,
        _pull_request_number: int,
        *,
        post_comments: bool,
        progress_callback: Callable[[str], Awaitable[None]],
        expected_head_sha: str | None,
    ) -> ReviewResult:
        assert post_comments is False
        assert expected_head_sha is None
        await progress_callback("reviewing")
        self.started.set()
        await self.release.wait()
        await progress_callback("validating")
        return _review()


class _FailingReviewService:
    async def review_pull_request(
        self,
        _owner: str,
        _repository: str,
        _pull_request_number: int,
        *,
        post_comments: bool,
        progress_callback: Callable[[str], Awaitable[None]],
        expected_head_sha: str | None,
    ) -> ReviewResult:
        await progress_callback("reviewing")
        raise CodexRuntimeError()


def test_enqueue_cannot_race_behind_graceful_shutdown() -> None:
    async def scenario() -> None:
        store = InMemoryJobStore()
        service = _BlockingReviewService()
        queue = ReviewQueue(
            store,
            service,  # type: ignore[arg-type]
            concurrency=1,
        )
        await queue.start()
        await queue.enqueue(
            owner="octo",
            repository="example",
            pull_request_number=1,
            source=JobSource.MANUAL,
        )
        await service.started.wait()

        stopping = asyncio.create_task(queue.stop(graceful_timeout=2))
        await asyncio.sleep(0)
        assert not queue.is_running
        late_enqueue = asyncio.create_task(
            queue.enqueue(
                owner="octo",
                repository="example",
                pull_request_number=2,
                source=JobSource.MANUAL,
            )
        )
        await asyncio.sleep(0)
        assert not late_enqueue.done()

        service.release.set()
        await stopping
        with pytest.raises(WorkerShutdownError):
            await late_enqueue
        assert len(await store.list_jobs()) == 1

    asyncio.run(scenario())


def test_worker_failure_becomes_safe_terminal_job() -> None:
    async def scenario() -> None:
        store = InMemoryJobStore()
        queue = ReviewQueue(
            store,
            _FailingReviewService(),  # type: ignore[arg-type]
            concurrency=1,
        )
        await queue.start()
        job = await queue.enqueue(
            owner="octo",
            repository="example",
            pull_request_number=1,
            source=JobSource.MANUAL,
        )
        await queue.join()
        failed = await store.require(job.job_id)
        await queue.stop()

        assert failed.status is JobStatus.FAILED
        assert failed.completed_at is not None
        assert failed.error == (
            "Codex could not run the review. Confirm that the CLI is authenticated."
        )

    asyncio.run(scenario())
