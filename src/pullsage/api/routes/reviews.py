"""Manual review submission and ephemeral job lookup endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from pullsage.api.dependencies import (
    get_github_client,
    get_job_store,
    get_review_queue,
)
from pullsage.github.client import GitHubClient
from pullsage.jobs.models import JobSource, JobStatus, ReviewJob
from pullsage.jobs.store import InMemoryJobStore
from pullsage.jobs.worker import ReviewQueue

router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])


class ManualReviewRequest(BaseModel):
    """Validated input for a user-authorized manual review."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner: str = Field(min_length=1, max_length=255)
    repository: str = Field(min_length=1, max_length=255)
    pull_request_number: int = Field(ge=1)
    post_comments: StrictBool = False


class ReviewJobAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    status: JobStatus
    deduplicated: bool
    message: str


@router.post(
    "",
    response_model=ReviewJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a pull-request review",
)
async def create_review(
    payload: ManualReviewRequest,
    queue: ReviewQueue = Depends(get_review_queue),
    github_client: GitHubClient = Depends(get_github_client),
) -> ReviewJobAccepted:
    """Queue an asynchronous review; posting remains opt-in per request."""

    if not queue.is_running:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "queue_unavailable",
                "message": "The review queue is not available",
            },
        )
    pull_request = await github_client.get_pull_request(
        payload.owner,
        payload.repository,
        payload.pull_request_number,
    )
    job, created = await queue.enqueue_with_status(
        owner=payload.owner,
        repository=payload.repository,
        pull_request_number=payload.pull_request_number,
        source=JobSource.MANUAL,
        post_comments=payload.post_comments,
        head_sha=pull_request.head_sha,
    )
    return ReviewJobAccepted(
        job_id=job.job_id,
        status=job.status,
        deduplicated=not created,
        message=(
            "Review job accepted" if created else "An equivalent review job is already active"
        ),
    )


@router.get(
    "/{job_id}",
    response_model=ReviewJob,
    summary="Get a review job",
    responses={404: {"description": "Job not found or expired"}},
)
async def get_review(
    job_id: UUID,
    store: InMemoryJobStore = Depends(get_job_store),
) -> ReviewJob:
    """Return current status and any retained result for an in-memory job."""

    job = await store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "job_not_found",
                "message": "Review job was not found or has expired",
            },
        )
    return job
