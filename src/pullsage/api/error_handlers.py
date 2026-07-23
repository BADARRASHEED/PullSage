"""Consistent, non-sensitive JSON error responses for FastAPI."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pullsage.exceptions import PullSageError

logger = logging.getLogger(__name__)

_STATUS_BY_EXCEPTION_NAME: dict[str, int] = {
    "ConfigurationError": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MissingConfigurationError": status.HTTP_503_SERVICE_UNAVAILABLE,
    "WebhookSignatureError": status.HTTP_401_UNAUTHORIZED,
    "InvalidWebhookSignatureError": status.HTTP_401_UNAUTHORIZED,
    "GitHubAuthenticationError": status.HTTP_502_BAD_GATEWAY,
    "GitHubRateLimitError": status.HTTP_503_SERVICE_UNAVAILABLE,
    "GitHubNotFoundError": status.HTTP_404_NOT_FOUND,
    "GitHubAPIError": status.HTTP_502_BAD_GATEWAY,
    "PullRequestTooLargeError": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    "CodexNotFoundError": status.HTTP_503_SERVICE_UNAVAILABLE,
    "CodexUnavailableError": status.HTTP_503_SERVICE_UNAVAILABLE,
    "CodexAuthenticationError": status.HTTP_502_BAD_GATEWAY,
    "CodexTimeoutError": status.HTTP_504_GATEWAY_TIMEOUT,
    "CodexRuntimeError": status.HTTP_502_BAD_GATEWAY,
    "InvalidAIOutputError": status.HTTP_502_BAD_GATEWAY,
    "ReviewPostingError": status.HTTP_502_BAD_GATEWAY,
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def _error_code(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id(request),
        }
    }
    if details is not None:
        payload["error"]["details"] = details
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={"X-Request-ID": _request_id(request)},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register expected and fallback exception translations."""

    @app.exception_handler(HTTPException)
    async def handle_http_exception(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        if isinstance(exc.detail, dict):
            message = str(exc.detail.get("message", "Request failed"))
            code = str(exc.detail.get("code", "http_error"))
            details = exc.detail.get("details")
        else:
            message = str(exc.detail)
            code = "http_error"
            details = None
        return error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
            details=details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        details = [
            {
                "location": list(error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return error_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="validation_error",
            message="Request validation failed",
            details=details,
        )

    @app.exception_handler(PullSageError)
    async def handle_pullsage_error(
        request: Request,
        exc: PullSageError,
    ) -> JSONResponse:
        exception_name = type(exc).__name__
        status_code = getattr(
            exc,
            "status_code",
            _STATUS_BY_EXCEPTION_NAME.get(
                exception_name,
                status.HTTP_400_BAD_REQUEST,
            ),
        )
        code = getattr(exc, "code", _error_code(exception_name))
        message = getattr(exc, "safe_message", None) or str(exc)
        return error_response(
            request,
            status_code=int(status_code),
            code=str(code),
            message=str(message) or "PullSage request failed",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.exception(
            "Unhandled API error",
            extra={
                "event": "unhandled_api_error",
                "request_id": _request_id(request),
            },
        )
        return error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="An unexpected error occurred",
        )

