"""PullSage FastAPI application factory."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pullsage.api.dependencies import (
    ServiceFactory,
    build_delivery_cache,
    build_job_runtime,
    resolve_service_bundle,
)
from pullsage.api.error_handlers import register_error_handlers
from pullsage.api.middleware import RequestIDMiddleware
from pullsage.api.routes import api_router
from pullsage.api.routes.health import router as health_router
from pullsage.api.routes.webhooks import router as webhooks_router
from pullsage.config import Settings

logger = logging.getLogger(__name__)


async def _close_if_supported(resource: object) -> None:
    close = getattr(resource, "aclose", None)
    if close is None:
        close = getattr(resource, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def create_app(
    settings: Settings | None = None,
    *,
    service_factory: ServiceFactory | None = None,
) -> FastAPI:
    """Create an isolated PullSage application and runtime dependency graph."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_settings = settings or Settings()
        bundle = await resolve_service_bundle(
            runtime_settings,
            service_factory,
        )
        job_store, review_queue = build_job_runtime(
            runtime_settings,
            bundle.review_service,
        )
        application.state.settings = runtime_settings
        application.state.github_client = bundle.github_client
        application.state.codex_runner = bundle.codex_runner
        application.state.review_service = bundle.review_service
        application.state.job_store = job_store
        application.state.review_queue = review_queue
        application.state.delivery_cache = build_delivery_cache(
            runtime_settings
        )

        await review_queue.start()
        logger.info(
            "PullSage API started",
            extra={"event": "api_started"},
        )
        try:
            yield
        finally:
            await review_queue.stop()
            await _close_if_supported(bundle.github_client)
            logger.info(
                "PullSage API stopped",
                extra={"event": "api_stopped"},
            )

    application = FastAPI(
        title="PullSage API",
        summary="Insight before merge.",
        description=(
            "Queue AI-assisted GitHub pull-request reviews and receive verified "
            "GitHub webhooks. Repository write operations are disabled by "
            "default."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    application.add_middleware(RequestIDMiddleware)
    register_error_handlers(application)
    application.include_router(health_router)
    application.include_router(api_router)
    application.include_router(webhooks_router)
    return application


app = create_app()
