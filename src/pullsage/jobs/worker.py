"""Async in-memory review queue and background workers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import UUID

from pullsage.exceptions import (
    JobNotFoundError,
    PullSageError,
    ReviewCapacityError,
    WorkerShutdownError,
)
from pullsage.jobs.models import JobSource, JobStatus, ReviewJob
from pullsage.jobs.store import (
    InMemoryJobStore,
    InvalidJobTransitionError,
)
from pullsage.logging_config import reset_job_id, set_job_id

if TYPE_CHECKING:
    from pullsage.reviews.service import ReviewService

logger = logging.getLogger(__name__)

_STOP = object()


class ReviewQueue:
    """Coordinate background reviews using only ``asyncio`` primitives."""

    def __init__(
        self,
        store: InMemoryJobStore,
        review_service: ReviewService,
        *,
        concurrency: int = 2,
        retention_seconds: float | None = None,
        cleanup_interval_seconds: float | None = None,
        max_queue_size: int = 100,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self.store = store
        self.review_service = review_service
        self.concurrency = concurrency
        self.retention_seconds = (
            store.retention_seconds if retention_seconds is None else float(retention_seconds)
        )
        default_interval = max(
            1.0,
            min(60.0, self.retention_seconds / 2 or 1.0),
        )
        self.cleanup_interval_seconds = (
            default_interval
            if cleanup_interval_seconds is None
            else max(0.1, float(cleanup_interval_seconds))
        )
        self.max_queue_size = max_queue_size
        self._queue: asyncio.Queue[UUID | object] = asyncio.Queue(maxsize=max_queue_size)
        self._workers: list[asyncio.Task[None]] = []
        self._cleanup_task: asyncio.Task[None] | None = None
        self._started = False
        self._accepting = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Whether the queue has live worker tasks and accepts new jobs."""

        return (
            self._started
            and self._accepting
            and bool(self._workers)
            and all(not task.done() for task in self._workers)
        )

    @property
    def worker_count(self) -> int:
        return sum(not task.done() for task in self._workers)

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        """Start background workers and the completed-job cleanup loop."""

        async with self._lifecycle_lock:
            if self._started:
                return
            self._started = True
            self._accepting = True
            self._workers = [
                asyncio.create_task(
                    self._worker_loop(index),
                    name=f"pullsage-review-worker-{index + 1}",
                )
                for index in range(self.concurrency)
            ]
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="pullsage-job-cleanup",
            )
            logger.info(
                "Review workers started",
                extra={
                    "event": "review_workers_started",
                    "worker_count": self.concurrency,
                },
            )

    async def stop(self, *, graceful_timeout: float = 10.0) -> None:
        """Stop accepting work, drain when possible, and cancel safely."""

        async with self._lifecycle_lock:
            if not self._started:
                return
            self._accepting = False
            cleanup_task = self._cleanup_task
            self._cleanup_task = None
            if cleanup_task is not None:
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup_task

            drained = False
            try:
                await asyncio.wait_for(
                    self._queue.join(),
                    timeout=max(0.0, graceful_timeout),
                )
                drained = True
            except TimeoutError:
                logger.warning(
                    "Timed out while draining review queue",
                    extra={"event": "review_queue_drain_timeout"},
                )

            if drained:
                for _worker in self._workers:
                    await self._queue.put(_STOP)
                await asyncio.gather(*self._workers, return_exceptions=True)
            else:
                for worker in self._workers:
                    worker.cancel()
                await asyncio.gather(*self._workers, return_exceptions=True)

            self._workers.clear()
            self._started = False
            logger.info(
                "Review workers stopped",
                extra={"event": "review_workers_stopped"},
            )

    async def enqueue_with_status(
        self,
        *,
        owner: str,
        repository: str,
        pull_request_number: int,
        source: JobSource | str,
        post_comments: bool = False,
        head_sha: str | None = None,
    ) -> tuple[ReviewJob, bool]:
        """Create and enqueue a job, returning ``(job, created)``."""

        # Serialize the short admission section with start/stop so an accepted
        # job cannot land behind worker shutdown sentinels.
        async with self._lifecycle_lock:
            if not self.is_running:
                raise WorkerShutdownError()
            job, created = await self.store.get_or_create_job(
                owner=owner,
                repository=repository,
                pull_request_number=pull_request_number,
                source=source,
                post_comments=post_comments,
                head_sha=head_sha,
            )
            if created:
                try:
                    self._queue.put_nowait(job.job_id)
                except asyncio.QueueFull as error:
                    await self.store.delete(job.job_id)
                    raise ReviewCapacityError() from error
                logger.info(
                    "Review job queued",
                    extra={
                        "event": "review_job_queued",
                        "job_id": str(job.job_id),
                        "repository": f"{job.owner}/{job.repository}",
                        "pull_request_number": job.pull_request_number,
                        "source": job.source.value,
                    },
                )
        return job, created

    async def enqueue(self, **kwargs: object) -> ReviewJob:
        """Queue a job and return its current representation."""

        job, _created = await self.enqueue_with_status(**kwargs)  # type: ignore[arg-type]
        return job

    async def join(self) -> None:
        """Wait until all currently queued jobs have been handled."""

        await self._queue.join()

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            queued_item = await self._queue.get()
            try:
                if queued_item is _STOP:
                    return
                await self._process_job(queued_item)  # type: ignore[arg-type]
            except asyncio.CancelledError:
                if isinstance(queued_item, UUID):
                    with suppress(
                        JobNotFoundError,
                        InvalidJobTransitionError,
                    ):
                        await self.store.fail(
                            queued_item,
                            "Review worker stopped before completion",
                        )
                raise
            except Exception:
                logger.exception(
                    "Unexpected review worker failure",
                    extra={
                        "event": "review_worker_failure",
                        "worker_index": worker_index,
                    },
                )
            finally:
                self._queue.task_done()

    async def _process_job(self, job_id: UUID) -> None:
        token = set_job_id(str(job_id))
        try:
            await self._process_job_with_context(job_id)
        finally:
            reset_job_id(token)

    async def _process_job_with_context(self, job_id: UUID) -> None:
        job = await self.store.require(job_id)
        try:
            await self.store.transition(job_id, JobStatus.FETCHING_CONTEXT)

            async def update_progress(status: str) -> None:
                await self.store.transition(job_id, JobStatus(status))

            result = await self.review_service.review_pull_request(
                job.owner,
                job.repository,
                job.pull_request_number,
                post_comments=job.post_comments,
                progress_callback=update_progress,
                expected_head_sha=job.head_sha,
            )
            await self.store.transition(
                job_id,
                JobStatus.COMPLETED,
                result=result,
            )
            logger.info(
                "Review job completed",
                extra={
                    "event": "review_job_completed",
                    "job_id": str(job_id),
                    "repository": f"{job.owner}/{job.repository}",
                    "pull_request_number": job.pull_request_number,
                },
            )
        except asyncio.CancelledError:
            with suppress(JobNotFoundError, InvalidJobTransitionError):
                await self.store.fail(
                    job_id,
                    "Review worker stopped before completion",
                )
            raise
        except Exception as exc:
            error_message = self._safe_error_message(exc)
            with suppress(JobNotFoundError, InvalidJobTransitionError):
                await self.store.fail(job_id, error_message)
            logger.exception(
                "Review job failed",
                extra={
                    "event": "review_job_failed",
                    "job_id": str(job_id),
                    "repository": f"{job.owner}/{job.repository}",
                    "pull_request_number": job.pull_request_number,
                },
            )

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        if isinstance(exc, PullSageError):
            message = exc.safe_message.strip()
            if message:
                return message[:500]
        return "Review failed unexpectedly"

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval_seconds)
                removed = await self.store.cleanup_expired(retention_seconds=self.retention_seconds)
                if removed:
                    logger.info(
                        "Expired review jobs removed",
                        extra={
                            "event": "review_jobs_expired",
                            "removed_count": removed,
                        },
                    )
        except asyncio.CancelledError:
            raise
