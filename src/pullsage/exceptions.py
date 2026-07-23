"""Domain-specific exceptions with messages that are safe to expose.

The API, background workers, and MCP server all use this hierarchy.  Every
exception has a stable machine-readable ``code``, a suggested HTTP
``status_code``, and a ``safe_message`` that never contains response bodies,
credentials, diffs, or subprocess output.
"""

from __future__ import annotations

from typing import Any, ClassVar


class PullSageError(Exception):
    """Base class for expected PullSage failures.

    ``message`` may be useful for internal logging.  Callers must use
    ``safe_message`` in HTTP and MCP responses.
    """

    default_code: ClassVar[str] = "pullsage_error"
    default_status_code: ClassVar[int] = 500
    default_safe_message: ClassVar[str] = "PullSage could not complete the request."

    def __init__(
        self,
        message: str | None = None,
        *,
        safe_message: str | None = None,
        code: str | None = None,
        status_code: int | None = None,
        **details: Any,
    ) -> None:
        resolved_safe_message = (
            safe_message
            or message
            or self.default_safe_message
        )
        internal_message = message or resolved_safe_message
        super().__init__(internal_message)
        self.safe_message = resolved_safe_message
        self.code = code or self.default_code
        self.status_code = status_code or self.default_status_code
        self.details = details


class ConfigurationError(PullSageError):
    """Required runtime configuration is missing or invalid."""

    default_code = "configuration_error"
    default_status_code = 503
    default_safe_message = "PullSage is not configured for this operation."


class WebhookSignatureError(PullSageError):
    """A webhook did not carry a valid GitHub signature."""

    default_code = "invalid_webhook_signature"
    default_status_code = 401
    default_safe_message = "The webhook signature is invalid."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or self.default_safe_message,
            safe_message=self.default_safe_message,
        )


# A descriptive compatibility name for callers that prefer the adjective form.
InvalidWebhookSignatureError = WebhookSignatureError


class DuplicateDeliveryError(PullSageError):
    """A GitHub delivery identifier was already accepted recently."""

    default_code = "duplicate_webhook_delivery"
    default_status_code = 409
    default_safe_message = "This webhook delivery was already accepted."

    def __init__(self, delivery_id: str | None = None) -> None:
        super().__init__(
            self.default_safe_message,
            delivery_id=delivery_id,
        )
        self.delivery_id = delivery_id


class UnsupportedWebhookEventError(PullSageError):
    """The webhook event or pull-request action is intentionally unsupported."""

    default_code = "unsupported_webhook_event"
    default_status_code = 422
    default_safe_message = "This webhook event is not supported."

    def __init__(
        self,
        event: str | None = None,
        action: str | None = None,
    ) -> None:
        super().__init__(
            self.default_safe_message,
            event=event,
            action=action,
        )
        self.event = event
        self.action = action


class DraftPullRequestError(PullSageError):
    """A draft pull request was excluded from automated review."""

    default_code = "draft_pull_request"
    default_status_code = 422
    default_safe_message = "Draft pull requests are not reviewed automatically."


class GitHubError(PullSageError):
    """Base class for expected GitHub integration failures."""

    default_code = "github_error"
    default_status_code = 502
    default_safe_message = "The GitHub operation failed."


class GitHubAPIError(GitHubError):
    """GitHub returned an unexpected response or could not be reached."""

    default_code = "github_api_error"
    default_status_code = 502
    default_safe_message = "GitHub could not complete the request."

    def __init__(
        self,
        message: str | None = None,
        *,
        upstream_status_code: int | None = None,
        request_id: str | None = None,
        safe_message: str | None = None,
    ) -> None:
        super().__init__(
            message or safe_message or self.default_safe_message,
            safe_message=safe_message or self.default_safe_message,
            upstream_status_code=upstream_status_code,
            request_id=request_id,
        )
        self.upstream_status_code = upstream_status_code
        self.request_id = request_id


class GitHubAuthenticationError(GitHubAPIError):
    """GitHub rejected or was not given usable credentials."""

    default_code = "github_authentication_error"
    default_status_code = 502
    default_safe_message = "GitHub authentication failed."

    def __init__(
        self,
        message: str | None = None,
        *,
        upstream_status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            message or self.default_safe_message,
            upstream_status_code=upstream_status_code,
            request_id=request_id,
            safe_message=self.default_safe_message,
        )
        self.code = self.default_code


class GitHubRateLimitError(GitHubAPIError):
    """GitHub rate-limited the request."""

    default_code = "github_rate_limit_error"
    default_status_code = 503
    default_safe_message = "GitHub rate limit exceeded; try again later."

    def __init__(
        self,
        retry_after: int | None = None,
        *,
        upstream_status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            self.default_safe_message,
            upstream_status_code=upstream_status_code,
            request_id=request_id,
            safe_message=self.default_safe_message,
        )
        self.code = self.default_code
        self.status_code = self.default_status_code
        self.retry_after = retry_after
        self.details["retry_after"] = retry_after


class GitHubNotFoundError(GitHubAPIError):
    """The requested GitHub repository or pull request was not found."""

    default_code = "github_not_found"
    default_status_code = 404
    default_safe_message = "The requested GitHub resource was not found."

    def __init__(
        self,
        message: str | None = None,
        *,
        upstream_status_code: int | None = 404,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            message or self.default_safe_message,
            upstream_status_code=upstream_status_code,
            request_id=request_id,
            safe_message=self.default_safe_message,
        )
        self.code = self.default_code
        self.status_code = self.default_status_code


class PullRequestTooLargeError(GitHubError):
    """The configured pull-request context limit was exceeded."""

    default_code = "pull_request_too_large"
    default_status_code = 413
    default_safe_message = "The pull request is too large to review safely."

    def __init__(
        self,
        *,
        resource: str = "pull request",
        actual: int | None = None,
        limit: int | None = None,
    ) -> None:
        super().__init__(
            self.default_safe_message,
            resource=resource,
            actual=actual,
            limit=limit,
        )
        self.resource = resource
        self.actual = actual
        self.limit = limit


class TooManyChangedFilesError(PullRequestTooLargeError):
    """The pull request contains more changed files than configured."""

    default_code = "too_many_changed_files"

    def __init__(self, actual: int | None, limit: int) -> None:
        super().__init__(
            resource="changed files",
            actual=actual,
            limit=limit,
        )
        self.code = self.default_code


class CodexError(PullSageError):
    """Base class for expected local Codex runtime failures."""

    default_code = "codex_error"
    default_status_code = 502
    default_safe_message = "Codex could not complete the review."


class CodexNotFoundError(CodexError):
    """The configured Codex executable is not available."""

    default_code = "codex_not_found"
    default_status_code = 503
    default_safe_message = (
        "Codex CLI is not installed or is not available on PATH. "
        "Install and authenticate it before running reviews."
    )

    def __init__(self, command: str | None = None) -> None:
        super().__init__(
            self.default_safe_message,
            command=command,
        )
        self.command = command


class CodexTimeoutError(CodexError):
    """Codex exceeded the configured review timeout."""

    default_code = "codex_timeout"
    default_status_code = 504
    default_safe_message = "Codex exceeded the configured review timeout."

    def __init__(self, timeout_seconds: float | int | None = None) -> None:
        super().__init__(
            self.default_safe_message,
            timeout_seconds=timeout_seconds,
        )
        self.timeout_seconds = timeout_seconds


class CodexRuntimeError(CodexError):
    """Codex exited unsuccessfully or could not be started."""

    default_code = "codex_runtime_error"
    default_safe_message = (
        "Codex could not run the review. Confirm that the CLI is authenticated."
    )

    def __init__(
        self,
        message: str | None = None,
        *,
        return_code: int | None = None,
    ) -> None:
        super().__init__(
            message or self.default_safe_message,
            safe_message=self.default_safe_message,
            return_code=return_code,
        )
        self.return_code = return_code


class InvalidCodexOutputError(CodexError):
    """Codex did not return a result matching the review schema."""

    default_code = "invalid_codex_output"
    default_status_code = 502
    default_safe_message = "Codex returned an invalid structured review."

    def __init__(
        self,
        message: str | None = None,
        *,
        validation_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            message or self.default_safe_message,
            safe_message=self.default_safe_message,
            validation_errors=validation_errors or [],
        )
        self.validation_errors = validation_errors or []


class ReviewValidationError(PullSageError):
    """A review payload failed domain validation."""

    default_code = "review_validation_error"
    default_status_code = 422
    default_safe_message = "The review payload is invalid."

    def __init__(
        self,
        message: str | None = None,
        *,
        validation_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            message or self.default_safe_message,
            safe_message=self.default_safe_message,
            validation_errors=validation_errors or [],
        )
        self.validation_errors = validation_errors or []


class ReviewPostingError(PullSageError):
    """A validated review could not be posted to GitHub."""

    default_code = "review_posting_error"
    default_status_code = 502
    default_safe_message = "The review could not be posted to GitHub."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or self.default_safe_message,
            safe_message=self.default_safe_message,
        )


# Compatibility with the noun order used by some integrations.
GitHubReviewPostingError = ReviewPostingError


class JobNotFoundError(PullSageError):
    """An in-memory review job does not exist (or has expired)."""

    default_code = "job_not_found"
    default_status_code = 404
    default_safe_message = "The review job was not found."

    def __init__(self, job_id: str | None = None) -> None:
        super().__init__(self.default_safe_message, job_id=job_id)
        self.job_id = job_id


class DuplicateReviewJobError(PullSageError):
    """A review for the same pull-request head is already active."""

    default_code = "duplicate_review_job"
    default_status_code = 409
    default_safe_message = "A review for this pull request is already active."

    def __init__(
        self,
        *,
        existing_job_id: str | None = None,
        head_sha: str | None = None,
    ) -> None:
        super().__init__(
            self.default_safe_message,
            existing_job_id=existing_job_id,
            head_sha=head_sha,
        )
        self.existing_job_id = existing_job_id
        self.head_sha = head_sha


class WorkerShutdownError(PullSageError):
    """The review worker is shutting down and cannot accept work."""

    default_code = "worker_shutdown"
    default_status_code = 503
    default_safe_message = "The review worker is shutting down."

