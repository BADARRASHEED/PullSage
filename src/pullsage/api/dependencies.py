"""FastAPI dependency accessors and shared service construction."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from fastapi import HTTPException, Request, status

from pullsage.ai.codex_runner import CodexRunner
from pullsage.config import Settings
from pullsage.github.client import GitHubClient
from pullsage.github.webhook_security import DeliveryCache
from pullsage.jobs.store import InMemoryJobStore
from pullsage.jobs.worker import ReviewQueue
from pullsage.reviews.service import ReviewService


def setting_value(
    settings: Settings,
    name: str,
    *,
    prefixed_name: str | None = None,
    default: Any = None,
) -> Any:
    """Read a setting while tolerating the service layer's legacy aliases."""

    if hasattr(settings, name):
        return getattr(settings, name)
    if prefixed_name and hasattr(settings, prefixed_name):
        return getattr(settings, prefixed_name)
    return default


@dataclass(slots=True)
class ServiceBundle:
    """Runtime dependencies shared by HTTP routes and background workers."""

    github_client: GitHubClient
    codex_runner: CodexRunner
    review_service: ReviewService


ServiceFactory = Callable[
    [Settings],
    ServiceBundle
    | tuple[GitHubClient, CodexRunner, ReviewService]
    | Awaitable[ServiceBundle | tuple[GitHubClient, CodexRunner, ReviewService]],
]


def default_service_factory(settings: Settings) -> ServiceBundle:
    """Construct the production shared service graph."""

    github_client = GitHubClient(settings)
    codex_runner = CodexRunner(settings)
    review_service = ReviewService(settings, github_client, codex_runner)
    return ServiceBundle(
        github_client=github_client,
        codex_runner=codex_runner,
        review_service=review_service,
    )


async def resolve_service_bundle(
    settings: Settings,
    factory: ServiceFactory | None,
) -> ServiceBundle:
    """Resolve a synchronous or asynchronous test/production factory."""

    produced = (factory or default_service_factory)(settings)
    if inspect.isawaitable(produced):
        produced = await produced
    if isinstance(produced, ServiceBundle):
        return produced
    if isinstance(produced, tuple) and len(produced) == 3:
        return ServiceBundle(
            github_client=produced[0],
            codex_runner=produced[1],
            review_service=produced[2],
        )
    raise TypeError(
        "service_factory must return ServiceBundle or (GitHubClient, CodexRunner, ReviewService)"
    )


def build_job_runtime(
    settings: Settings,
    review_service: ReviewService,
) -> tuple[InMemoryJobStore, ReviewQueue]:
    """Create the process-local job store and review queue."""

    retention = float(
        setting_value(
            settings,
            "job_retention_seconds",
            prefixed_name="pullsage_job_retention_seconds",
            default=3600,
        )
    )
    concurrency = int(
        setting_value(
            settings,
            "max_concurrent_reviews",
            prefixed_name="pullsage_max_concurrent_reviews",
            default=2,
        )
    )
    store = InMemoryJobStore(retention_seconds=retention)
    queue = ReviewQueue(
        store,
        review_service,
        concurrency=concurrency,
        retention_seconds=retention,
    )
    return store, queue


def build_delivery_cache(settings: Settings) -> DeliveryCache:
    """Create the bounded process-local GitHub delivery cache."""

    return DeliveryCache(
        max_entries=int(
            setting_value(
                settings,
                "max_webhook_deliveries",
                default=10_000,
            )
        ),
        ttl_seconds=float(
            setting_value(
                settings,
                "delivery_retention_seconds",
                default=3600,
            )
        ),
    )


def _state_dependency(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PullSage is still starting",
        )
    return value


def get_settings(request: Request) -> Settings:
    return cast(Settings, _state_dependency(request, "settings"))


def get_github_client(request: Request) -> GitHubClient:
    return cast(GitHubClient, _state_dependency(request, "github_client"))


def get_codex_runner(request: Request) -> CodexRunner:
    return cast(CodexRunner, _state_dependency(request, "codex_runner"))


def get_review_service(request: Request) -> ReviewService:
    return cast(ReviewService, _state_dependency(request, "review_service"))


def get_job_store(request: Request) -> InMemoryJobStore:
    return cast(InMemoryJobStore, _state_dependency(request, "job_store"))


def get_review_queue(request: Request) -> ReviewQueue:
    return cast(ReviewQueue, _state_dependency(request, "review_queue"))


def get_delivery_cache(request: Request) -> DeliveryCache:
    return cast(DeliveryCache, _state_dependency(request, "delivery_cache"))
