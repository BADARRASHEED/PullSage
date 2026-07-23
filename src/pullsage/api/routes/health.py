"""Liveness and degraded-readiness endpoints."""

from __future__ import annotations

import inspect
import shutil
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from pullsage.ai.codex_runner import CodexRunner
from pullsage.api.dependencies import (
    get_codex_runner,
    get_review_queue,
    get_settings,
    setting_value,
)
from pullsage.config import Settings
from pullsage.jobs.worker import ReviewQueue

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["healthy"]
    service: Literal["pullsage"]


class ReadinessChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings_loaded: bool
    worker_running: bool
    github_token_configured: bool
    webhook_secret_configured: bool
    codex_available: bool


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "degraded"]
    checks: ReadinessChecks


def _secret_is_configured(value: Any) -> bool:
    if value is None:
        return False
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return bool(str(value).strip())


async def _codex_available(
    runner: CodexRunner,
    settings: Settings,
) -> bool:
    for attribute_name in ("is_available", "available"):
        attribute = getattr(runner, attribute_name, None)
        if attribute is None:
            continue
        try:
            value = attribute() if callable(attribute) else attribute
            if inspect.isawaitable(value):
                value = await value
            return bool(value)
        except (OSError, RuntimeError):
            return False
    command = str(setting_value(settings, "codex_command", default="codex"))
    return shutil.which(command) is not None


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Process liveness",
)
async def health() -> HealthResponse:
    """Return basic process health without inspecting external dependencies."""

    return HealthResponse(status="healthy", service="pullsage")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Runtime readiness",
    responses={503: {"model": ReadinessResponse}},
)
async def ready(
    response: Response,
    settings: Settings = Depends(get_settings),
    queue: ReviewQueue = Depends(get_review_queue),
    codex_runner: CodexRunner = Depends(get_codex_runner),
) -> ReadinessResponse:
    """Report each readiness prerequisite without exposing secret values."""

    checks = ReadinessChecks(
        settings_loaded=True,
        worker_running=queue.is_running,
        github_token_configured=_secret_is_configured(
            setting_value(settings, "github_token")
        ),
        webhook_secret_configured=_secret_is_configured(
            setting_value(settings, "github_webhook_secret")
        ),
        codex_available=await _codex_available(codex_runner, settings),
    )
    is_ready = all(
        (
            checks.settings_loaded,
            checks.worker_running,
            checks.github_token_configured,
            checks.webhook_secret_configured,
            checks.codex_available,
        )
    )
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if is_ready else "degraded",
        checks=checks,
    )
