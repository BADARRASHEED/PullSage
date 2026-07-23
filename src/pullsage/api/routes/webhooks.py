"""Verified, deduplicated GitHub webhook ingestion."""

from __future__ import annotations

import inspect
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pullsage.api.dependencies import (
    get_delivery_cache,
    get_review_queue,
    get_settings,
    setting_value,
)
from pullsage.config import Settings
from pullsage.github.webhook_security import (
    DeliveryCache,
    is_supported_pull_request_action,
    should_process_pull_request,
    verify_webhook_signature,
)
from pullsage.jobs.models import JobSource
from pullsage.jobs.worker import ReviewQueue

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class _WebhookOwner(BaseModel):
    model_config = ConfigDict(extra="ignore")

    login: str = Field(min_length=1)


class _WebhookRepository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    owner: _WebhookOwner


class _WebhookHead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str = Field(min_length=1, max_length=128)


class _WebhookPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    draft: bool = False
    head: _WebhookHead


class GitHubPullRequestWebhook(BaseModel):
    """Only the verified webhook fields PullSage needs to enqueue a review."""

    model_config = ConfigDict(extra="ignore")

    action: str
    number: int = Field(ge=1)
    repository: _WebhookRepository
    pull_request: _WebhookPullRequest


class WebhookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted", "ignored", "duplicate"]
    reason: str
    job_id: UUID | None = None


def _secret_value(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return str(value)


async def _check_and_store_delivery(
    cache: DeliveryCache,
    delivery_id: str,
) -> bool:
    accepted = cache.check_and_store(delivery_id)
    if inspect.isawaitable(accepted):
        accepted = await accepted
    return bool(accepted)


@router.post(
    "/github",
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive a GitHub pull-request webhook",
)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    queue: ReviewQueue = Depends(get_review_queue),
    delivery_cache: DeliveryCache = Depends(get_delivery_cache),
) -> WebhookResponse:
    """Verify the raw body before parsing, then deduplicate and enqueue."""

    body = await request.body()
    secret = _secret_value(
        setting_value(settings, "github_webhook_secret", default=None)
    )
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "webhook_not_configured",
                "message": "GitHub webhook verification is not configured",
            },
        )
    if not x_hub_signature_256:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_webhook_signature",
                "message": "Webhook signature is missing or invalid",
            },
        )

    try:
        verified = verify_webhook_signature(
            body,
            x_hub_signature_256,
            secret,
        )
    except Exception as exc:
        if "signature" not in type(exc).__name__.casefold():
            raise
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_webhook_signature",
                "message": "Webhook signature is missing or invalid",
            },
        ) from exc
    if verified is False:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_webhook_signature",
                "message": "Webhook signature is missing or invalid",
            },
        )
    if not x_github_event:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_github_event",
                "message": "X-GitHub-Event is required",
            },
        )
    if not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_github_delivery",
                "message": "X-GitHub-Delivery is required",
            },
        )
    if x_github_event != "pull_request":
        return WebhookResponse(
            status="ignored",
            reason="Unsupported GitHub event",
        )

    try:
        raw_payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_webhook_payload",
                "message": "Webhook payload is malformed or incomplete",
            },
        ) from exc
    if not isinstance(raw_payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_webhook_payload",
                "message": "Webhook payload is malformed or incomplete",
            },
        )
    if not is_supported_pull_request_action(raw_payload.get("action")):
        return WebhookResponse(
            status="ignored",
            reason="Unsupported pull-request action",
        )
    try:
        payload = GitHubPullRequestWebhook.model_validate(raw_payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_webhook_payload",
                "message": "Webhook payload is malformed or incomplete",
            },
        ) from exc
    if not should_process_pull_request(
        x_github_event,
        payload.action,
        payload.pull_request.draft,
    ):
        return WebhookResponse(
            status="ignored",
            reason="Draft pull requests are ignored until ready for review",
        )
    if not queue.is_running:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "queue_unavailable",
                "message": "The review queue is not available",
            },
        )
    if not await _check_and_store_delivery(delivery_cache, x_github_delivery):
        return WebhookResponse(
            status="duplicate",
            reason="GitHub delivery was already processed",
        )

    try:
        job, created = await queue.enqueue_with_status(
            owner=payload.repository.owner.login,
            repository=payload.repository.name,
            pull_request_number=payload.number,
            source=JobSource.WEBHOOK,
            post_comments=bool(
                setting_value(
                    settings,
                    "post_comments",
                    prefixed_name="pullsage_post_comments",
                    default=False,
                )
            ),
            head_sha=payload.pull_request.head.sha,
        )
    except Exception:
        delivery_cache.discard(x_github_delivery)
        raise
    return WebhookResponse(
        status="accepted" if created else "duplicate",
        reason=(
            "Review job accepted"
            if created
            else "An equivalent pull-request head is already queued"
        ),
        job_id=job.job_id,
    )
