"""Shared pull-request review orchestration used by FastAPI and MCP."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from pullsage.exceptions import (
    GitHubAPIError,
    GitHubAuthenticationError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    ReviewPostingError,
    StalePullRequestHeadError,
    TooManyChangedFilesError,
)
from pullsage.github.client import GitHubClient
from pullsage.github.models import ChangedFile, PostedReview
from pullsage.reviews.formatter import (
    build_inline_comments,
    format_review_markdown,
    review_event_for_result,
)
from pullsage.reviews.models import (
    MAX_REVIEW_LIST_ITEMS,
    PullRequestContext,
    ReviewResult,
)
from pullsage.reviews.validation import (
    coerce_review_result,
    validate_and_filter_review,
)

logger = logging.getLogger(__name__)

ReviewProgressCallback = Callable[[str], Awaitable[None]]
_MAX_PULL_REQUEST_BODY_CHARS = 20_000


class CodexRunnerProtocol(Protocol):
    """Dependency-injection contract required by :class:`ReviewService`."""

    async def review(self, context: PullRequestContext) -> ReviewResult:
        """Return a schema-valid review of a bounded context."""


def _setting(source: object, names: Sequence[str], default: Any) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return default
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _truncate_at_line_boundary(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    bounded = value[:limit]
    last_newline = bounded.rfind("\n")
    if last_newline >= int(limit * 0.8):
        return bounded[: last_newline + 1]
    return bounded


class ReviewService:
    """Fetch, bound, review, validate, and optionally post one PR.

    The service owns no global state and does not close its injected
    dependencies. Both the API workers and MCP server use this same class.
    """

    def __init__(
        self,
        settings: object,
        github_client: GitHubClient,
        codex_runner: CodexRunnerProtocol,
    ) -> None:
        self.settings = settings
        self.github_client = github_client
        self.codex_runner = codex_runner
        self.min_confidence = float(
            _setting(
                settings,
                (
                    "min_confidence",
                    "pullsage_min_confidence",
                    "PULLSAGE_MIN_CONFIDENCE",
                ),
                0.8,
            )
        )
        self.max_diff_chars = int(
            _setting(
                settings,
                (
                    "max_diff_chars",
                    "pullsage_max_diff_chars",
                    "PULLSAGE_MAX_DIFF_CHARS",
                ),
                200_000,
            )
        )
        self.max_changed_files = int(
            _setting(
                settings,
                (
                    "max_changed_files",
                    "pullsage_max_changed_files",
                    "PULLSAGE_MAX_CHANGED_FILES",
                ),
                100,
            )
        )
        self.post_comments_by_default = bool(
            _setting(
                settings,
                (
                    "post_comments",
                    "pullsage_post_comments",
                    "PULLSAGE_POST_COMMENTS",
                ),
                False,
            )
        )
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("minimum review confidence must be between 0 and 1")
        if self.max_diff_chars <= 0:
            raise ValueError("maximum diff characters must be positive")
        if self.max_changed_files <= 0:
            raise ValueError("maximum changed files must be positive")

    def bound_changed_file_patches(
        self,
        changed_files: Sequence[ChangedFile],
    ) -> tuple[list[ChangedFile], bool]:
        """Bound the aggregate optional patch text included with the diff."""

        remaining = self.max_diff_chars
        bounded: list[ChangedFile] = []
        truncated = False
        for changed_file in changed_files:
            patch = changed_file.patch
            if patch is None:
                bounded.append(changed_file)
                continue
            if remaining <= 0:
                bounded.append(changed_file.model_copy(update={"patch": None}))
                truncated = True
                continue
            if len(patch) <= remaining:
                bounded.append(changed_file)
                remaining -= len(patch)
                continue
            bounded_patch = _truncate_at_line_boundary(patch, remaining)
            bounded.append(changed_file.model_copy(update={"patch": bounded_patch or None}))
            remaining = 0
            truncated = True
        return bounded, truncated

    async def fetch_context(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        expected_head_sha: str | None = None,
    ) -> PullRequestContext:
        """Fetch and sanitize all bounded context needed by Codex."""

        started = time.perf_counter()
        pull_request = await self.github_client.get_pull_request(
            owner,
            repository,
            pull_request_number,
        )
        self._require_expected_head(expected_head_sha, pull_request.head_sha)
        if pull_request.changed_files > self.max_changed_files:
            raise TooManyChangedFilesError(
                pull_request.changed_files,
                self.max_changed_files,
            )

        changed_files_result, diff_result = await asyncio.gather(
            self.github_client.get_changed_files(
                owner,
                repository,
                pull_request_number,
                max_files=self.max_changed_files,
            ),
            self.github_client.get_pull_request_diff(
                owner,
                repository,
                pull_request_number,
                max_chars=self.max_diff_chars,
                truncate=True,
            ),
        )
        changed_files, patches_truncated = self.bound_changed_file_patches(changed_files_result)
        await self._assert_current_head(
            owner,
            repository,
            pull_request_number,
            pull_request.head_sha,
        )
        warnings: list[str] = []
        pull_request_body = pull_request.body
        if pull_request_body is not None and len(pull_request_body) > _MAX_PULL_REQUEST_BODY_CHARS:
            pull_request_body = _truncate_at_line_boundary(
                pull_request_body,
                _MAX_PULL_REQUEST_BODY_CHARS,
            )
            warnings.append("The pull-request body was truncated to the safe context limit.")
        if diff_result.truncated:
            warnings.append(
                "The unified diff was truncated to the configured character "
                "limit; findings may be incomplete."
            )
        if patches_truncated:
            warnings.append(
                "Changed-file patch fragments were truncated to the configured "
                "aggregate character limit."
            )

        context = PullRequestContext(
            repository_full_name=pull_request.repository_full_name,
            pull_request_number=pull_request.number,
            title=pull_request.title,
            author_login=pull_request.author_login,
            base_branch=pull_request.base_ref,
            head_branch=pull_request.head_ref,
            head_sha=pull_request.head_sha,
            body=pull_request_body,
            state=pull_request.state.value,
            additions=pull_request.additions,
            deletions=pull_request.deletions,
            changed_files=changed_files,
            unified_diff=diff_result.content,
            diff_original_length=diff_result.original_length,
            diff_truncated=diff_result.truncated,
            warnings=warnings,
        )
        logger.info(
            "Pull-request context fetched",
            extra={
                "event": "review_context_fetched",
                "repository": pull_request.repository_full_name,
                "pull_request_number": pull_request.number,
                "duration_ms": round((time.perf_counter() - started) * 1_000),
                "changed_file_count": len(changed_files),
                "diff_truncated": diff_result.truncated,
            },
        )
        return context

    @staticmethod
    def _require_expected_head(
        expected_head_sha: str | None,
        actual_head_sha: str,
    ) -> None:
        if (
            expected_head_sha is not None
            and expected_head_sha.casefold() != actual_head_sha.casefold()
        ):
            raise StalePullRequestHeadError(
                expected_head_sha=expected_head_sha,
                actual_head_sha=actual_head_sha,
            )

    async def _assert_current_head(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> None:
        current = await self.github_client.get_pull_request(
            owner,
            repository,
            pull_request_number,
        )
        self._require_expected_head(expected_head_sha, current.head_sha)

    @staticmethod
    def _add_context_limitations(
        review: ReviewResult,
        warnings: Sequence[str],
    ) -> ReviewResult:
        original = list(review.limitations)
        existing = {" ".join(item.casefold().split()) for item in original}
        system_limitations: list[str] = []
        for warning in warnings:
            normalized = " ".join(warning.casefold().split())
            if normalized not in existing:
                system_limitations.append(warning)
                existing.add(normalized)
        if not system_limitations:
            return review
        system_limitations = system_limitations[:MAX_REVIEW_LIST_ITEMS]
        retained_count = MAX_REVIEW_LIST_ITEMS - len(system_limitations)
        limitations = [*original[:retained_count], *system_limitations]
        return ReviewResult.model_validate(
            {
                **review.model_dump(mode="python"),
                "limitations": limitations,
            }
        )

    async def review_pull_request(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        post_comments: bool | None = False,
        progress_callback: ReviewProgressCallback | None = None,
        expected_head_sha: str | None = None,
    ) -> ReviewResult:
        """Run a complete review; posting stays opt-in and defaults to dry-run."""

        if post_comments is not None and not isinstance(post_comments, bool):
            raise TypeError("post_comments must be a boolean or None")
        started = time.perf_counter()
        context = await self.fetch_context(
            owner,
            repository,
            pull_request_number,
            expected_head_sha=expected_head_sha,
        )

        if progress_callback is not None:
            await progress_callback("reviewing")
        codex_started = time.perf_counter()
        raw_review = await self.codex_runner.review(context)
        codex_duration_ms = round((time.perf_counter() - codex_started) * 1_000)

        await self._assert_current_head(
            owner,
            repository,
            pull_request_number,
            context.head_sha,
        )
        if progress_callback is not None:
            await progress_callback("validating")
        validation_started = time.perf_counter()
        review = validate_and_filter_review(
            coerce_review_result(raw_review),
            context.changed_files,
            min_confidence=self.min_confidence,
        )
        review = self._add_context_limitations(review, context.warnings)
        validation_duration_ms = round((time.perf_counter() - validation_started) * 1_000)

        should_post = self.post_comments_by_default if post_comments is None else post_comments
        posting_duration_ms = 0
        if should_post:
            if progress_callback is not None:
                await progress_callback("posting")
            posting_started = time.perf_counter()
            await self.post_review(
                owner,
                repository,
                pull_request_number,
                review,
                changed_files=context.changed_files,
                expected_head_sha=context.head_sha,
            )
            posting_duration_ms = round((time.perf_counter() - posting_started) * 1_000)

        logger.info(
            "Pull-request review completed",
            extra={
                "event": "review_completed",
                "repository": context.repository_full_name,
                "pull_request_number": pull_request_number,
                "duration_ms": round((time.perf_counter() - started) * 1_000),
                "codex_duration_ms": codex_duration_ms,
                "validation_duration_ms": validation_duration_ms,
                "posting_duration_ms": posting_duration_ms,
                "finding_count": len(review.findings),
                "posted": should_post,
                "status": "completed",
            },
        )
        return review

    async def run_review(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        post_comments: bool | None = False,
        progress_callback: ReviewProgressCallback | None = None,
        expected_head_sha: str | None = None,
    ) -> ReviewResult:
        """Compatibility alias for :meth:`review_pull_request`."""

        return await self.review_pull_request(
            owner,
            repository,
            pull_request_number,
            post_comments=post_comments,
            progress_callback=progress_callback,
            expected_head_sha=expected_head_sha,
        )

    async def post_review(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        review: ReviewResult | Mapping[str, Any],
        *,
        changed_files: Sequence[ChangedFile] | None = None,
        expected_head_sha: str | None = None,
    ) -> PostedReview:
        """Validate and post one summary with only safely mapped comments."""

        if expected_head_sha is None:
            pull_request = await self.github_client.get_pull_request(
                owner,
                repository,
                pull_request_number,
            )
            expected_head_sha = pull_request.head_sha
        if changed_files is None:
            changed_files = await self.github_client.get_changed_files(
                owner,
                repository,
                pull_request_number,
                max_files=self.max_changed_files,
            )
        await self._assert_current_head(
            owner,
            repository,
            pull_request_number,
            expected_head_sha,
        )
        validated = validate_and_filter_review(
            coerce_review_result(review),
            changed_files,
            min_confidence=self.min_confidence,
        )
        body = format_review_markdown(validated)
        comments = build_inline_comments(validated, changed_files)
        event = review_event_for_result(validated)
        try:
            return await self.github_client.post_pull_request_review(
                owner,
                repository,
                pull_request_number,
                body=body,
                event=event,
                comments=comments,
                commit_id=expected_head_sha,
            )
        except (
            GitHubAuthenticationError,
            GitHubRateLimitError,
            GitHubNotFoundError,
        ):
            raise
        except GitHubAPIError as exc:
            raise ReviewPostingError() from exc
