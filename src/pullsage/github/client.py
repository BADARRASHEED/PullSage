"""Asynchronous, narrowly scoped GitHub REST API client."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from pullsage.exceptions import (
    ConfigurationError,
    GitHubAPIError,
    GitHubAuthenticationError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    PullRequestTooLargeError,
    TooManyChangedFilesError,
)
from pullsage.github.models import (
    ChangedFile,
    ChangedFileStatus,
    GitHubReviewComment,
    PostedReview,
    PullRequest,
    PullRequestDiff,
    PullRequestState,
    ReviewEvent,
)

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CHANGED_FILES = 100
DEFAULT_MAX_DIFF_CHARS = 200_000
DEFAULT_MAX_RESPONSE_BYTES = 2_097_152
DEFAULT_USER_AGENT = "PullSage/0.1"
GITHUB_JSON_MEDIA_TYPE = "application/vnd.github+json"
GITHUB_DIFF_MEDIA_TYPE = "application/vnd.github.diff"


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


def _secret_value(value: object) -> object:
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else value


class GitHubClient:
    """A small async client for pull-request inspection and review posting.

    A caller may provide a settings-like object as the first argument, or pass
    explicit values. An injected ``httpx.AsyncClient`` makes the class easy to
    test without live network access.
    """

    def __init__(
        self,
        token: str | object | None = None,
        api_url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_changed_files: int = DEFAULT_MAX_CHANGED_FILES,
        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if token is not None and not isinstance(token, str):
            settings = token
            token = _secret_value(_setting(settings, ("github_token", "GITHUB_TOKEN"), None))
            api_url = api_url or _setting(
                settings,
                ("github_api_url", "GITHUB_API_URL"),
                DEFAULT_GITHUB_API_URL,
            )
            timeout = float(
                _setting(
                    settings,
                    ("github_timeout_seconds", "GITHUB_TIMEOUT_SECONDS"),
                    timeout,
                )
            )
            max_changed_files = int(
                _setting(
                    settings,
                    (
                        "pullsage_max_changed_files",
                        "max_changed_files",
                        "PULLSAGE_MAX_CHANGED_FILES",
                    ),
                    max_changed_files,
                )
            )
            max_diff_chars = int(
                _setting(
                    settings,
                    (
                        "pullsage_max_diff_chars",
                        "max_diff_chars",
                        "PULLSAGE_MAX_DIFF_CHARS",
                    ),
                    max_diff_chars,
                )
            )

        token = _secret_value(token)
        if token is not None and not isinstance(token, str):
            raise TypeError("GitHub token must be a string or None")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_changed_files <= 0:
            raise ValueError("max_changed_files must be positive")
        if max_diff_chars <= 0:
            raise ValueError("max_diff_chars must be positive")

        resolved_api_url = (api_url or DEFAULT_GITHUB_API_URL).rstrip("/")
        parsed_api_url = httpx.URL(resolved_api_url)
        if parsed_api_url.scheme not in {"http", "https"} or not parsed_api_url.host:
            raise ValueError("api_url must be an absolute HTTP(S) URL")

        self._token = token.strip() if token else None
        self.api_url = resolved_api_url
        self.timeout = float(timeout)
        self.max_changed_files = max_changed_files
        self.max_diff_chars = max_diff_chars
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None

    @property
    def is_configured(self) -> bool:
        """Whether a token is available (without exposing it)."""

        return bool(self._token)

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close an internally created HTTP client."""

        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    def _headers(self, accept: str) -> dict[str, str]:
        if not self._token:
            raise ConfigurationError(
                "GITHUB_TOKEN is required for GitHub API operations.",
                safe_message="GitHub authentication is not configured.",
                code="github_token_missing",
            )
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "User-Agent": DEFAULT_USER_AGENT,
            "X-GitHub-Api-Version": DEFAULT_GITHUB_API_VERSION,
        }

    @staticmethod
    def _repository_path(
        owner: str,
        repository: str,
        suffix: str,
    ) -> str:
        if not owner or not owner.strip() or not repository or not repository.strip():
            raise ValueError("owner and repository must be non-empty")
        if any(value in {".", ".."} for value in (owner.strip(), repository.strip())):
            raise ValueError("owner and repository are invalid")
        encoded_owner = quote(owner.strip(), safe="")
        encoded_repository = quote(repository.strip(), safe="")
        return f"/repos/{encoded_owner}/{encoded_repository}/{suffix.lstrip('/')}"

    @staticmethod
    def _validate_pull_number(pull_request_number: int) -> None:
        if (
            isinstance(pull_request_number, bool)
            or not isinstance(pull_request_number, int)
            or pull_request_number <= 0
        ):
            raise ValueError("pull_request_number must be a positive integer")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = GITHUB_JSON_MEDIA_TYPE,
        params: Mapping[str, str | int] | None = None,
        json_payload: Mapping[str, Any] | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        truncate_response: bool = False,
    ) -> httpx.Response:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        url = f"{self.api_url}{path}"
        try:
            request = self._client.build_request(
                method,
                url,
                headers=self._headers(accept),
                params=params,
                json=json_payload,
                timeout=self.timeout,
            )
            streamed_response = await self._client.send(request, stream=True)
            try:
                content, response_truncated = await self._read_response_bounded(
                    streamed_response,
                    max_response_bytes=max_response_bytes,
                    truncate=truncate_response,
                )
                response = httpx.Response(
                    streamed_response.status_code,
                    headers=streamed_response.headers,
                    content=content,
                    request=streamed_response.request,
                )
                response.extensions["pullsage_body_truncated"] = response_truncated
            finally:
                await streamed_response.aclose()
        except httpx.TimeoutException as exc:
            raise GitHubAPIError(
                "GitHub API request timed out.",
                safe_message="The GitHub request timed out.",
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubAPIError(
                f"GitHub API request failed ({type(exc).__name__}).",
                safe_message="GitHub could not be reached.",
            ) from exc

        if response.status_code >= 400:
            self._raise_api_error(response)
        return response

    @staticmethod
    async def _read_response_bounded(
        response: httpx.Response,
        *,
        max_response_bytes: int,
        truncate: bool,
    ) -> tuple[bytes, bool]:
        """Read a decoded HTTP body without permitting unbounded allocation."""

        content = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = max_response_bytes - len(content)
            if len(chunk) <= remaining:
                content.extend(chunk)
                continue
            if not truncate:
                raise PullRequestTooLargeError(
                    resource="GitHub API response",
                    actual=len(content) + len(chunk),
                    limit=max_response_bytes,
                )
            if remaining > 0:
                content.extend(chunk[:remaining])
            return bytes(content), True
        return bytes(content), False

    @staticmethod
    def _response_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if isinstance(payload, Mapping):
            message = payload.get("message")
            if isinstance(message, str):
                return message.casefold()
        return ""

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        retry_header = response.headers.get("Retry-After")
        if retry_header:
            try:
                return max(0, int(float(retry_header)))
            except ValueError:
                pass
        reset_header = response.headers.get("X-RateLimit-Reset")
        if reset_header:
            try:
                return max(0, int(float(reset_header) - time.time()))
            except ValueError:
                pass
        return None

    def _raise_api_error(self, response: httpx.Response) -> None:
        status = response.status_code
        request_id = response.headers.get("X-GitHub-Request-Id")
        message = self._response_message(response)
        remaining = response.headers.get("X-RateLimit-Remaining")
        if status == 429 or remaining == "0" or (status == 403 and "rate limit" in message):
            raise GitHubRateLimitError(
                self._retry_after(response),
                upstream_status_code=status,
                request_id=request_id,
            )
        if status == 401 or (
            status == 403
            and any(
                marker in message
                for marker in (
                    "bad credentials",
                    "requires authentication",
                    "resource not accessible by integration",
                )
            )
        ):
            raise GitHubAuthenticationError(
                upstream_status_code=status,
                request_id=request_id,
            )
        if status == 404:
            raise GitHubNotFoundError(
                upstream_status_code=status,
                request_id=request_id,
            )
        raise GitHubAPIError(
            f"GitHub API returned HTTP {status}.",
            upstream_status_code=status,
            request_id=request_id,
        )

    @staticmethod
    def _json_object(response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubAPIError(
                "GitHub API returned malformed JSON.",
                safe_message="GitHub returned an invalid response.",
            ) from exc
        if not isinstance(payload, Mapping):
            raise GitHubAPIError(
                "GitHub API returned a non-object response.",
                safe_message="GitHub returned an invalid response.",
            )
        return payload

    @staticmethod
    def _json_list(response: httpx.Response) -> list[Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubAPIError(
                "GitHub API returned malformed JSON.",
                safe_message="GitHub returned an invalid response.",
            ) from exc
        if not isinstance(payload, list):
            raise GitHubAPIError(
                "GitHub API returned a non-list response.",
                safe_message="GitHub returned an invalid response.",
            )
        return payload

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("timestamp must be a string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _pull_request_from_payload(
        payload: Mapping[str, Any],
        repository_full_name: str,
    ) -> PullRequest:
        try:
            return PullRequest(
                repository_full_name=repository_full_name,
                number=payload["number"],
                title=payload["title"],
                body=payload.get("body"),
                state=PullRequestState(payload["state"]),
                draft=payload["draft"],
                html_url=payload["html_url"],
                author_login=payload["user"]["login"],
                base_ref=payload["base"]["ref"],
                head_ref=payload["head"]["ref"],
                head_sha=payload["head"]["sha"],
                additions=payload["additions"],
                deletions=payload["deletions"],
                changed_files=payload["changed_files"],
                created_at=GitHubClient._parse_datetime(payload.get("created_at")),
                updated_at=GitHubClient._parse_datetime(payload.get("updated_at")),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise GitHubAPIError(
                "GitHub pull-request response failed schema validation.",
                safe_message="GitHub returned invalid pull-request metadata.",
            ) from exc

    @staticmethod
    def _changed_file_from_payload(payload: Any) -> ChangedFile:
        if not isinstance(payload, Mapping):
            raise GitHubAPIError(
                "GitHub changed-file response contained a non-object.",
                safe_message="GitHub returned invalid changed-file metadata.",
            )
        try:
            return ChangedFile(
                filename=payload["filename"],
                status=ChangedFileStatus(payload["status"]),
                additions=payload["additions"],
                deletions=payload["deletions"],
                changes=payload["changes"],
                sha=payload.get("sha"),
                previous_filename=payload.get("previous_filename"),
                blob_url=payload.get("blob_url"),
                raw_url=payload.get("raw_url"),
                contents_url=payload.get("contents_url"),
                patch=payload.get("patch"),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise GitHubAPIError(
                "GitHub changed-file response failed schema validation.",
                safe_message="GitHub returned invalid changed-file metadata.",
            ) from exc

    async def get_pull_request(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
    ) -> PullRequest:
        """Fetch sanitized pull-request metadata."""

        self._validate_pull_number(pull_request_number)
        path = self._repository_path(
            owner,
            repository,
            f"pulls/{pull_request_number}",
        )
        response = await self._request("GET", path)
        payload = self._json_object(response)
        return self._pull_request_from_payload(
            payload,
            f"{owner.strip()}/{repository.strip()}",
        )

    async def get_changed_files(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        max_files: int | None = None,
    ) -> list[ChangedFile]:
        """Fetch changed files across GitHub pagination with a hard limit."""

        self._validate_pull_number(pull_request_number)
        limit = self.max_changed_files if max_files is None else max_files
        if limit <= 0:
            raise ValueError("max_files must be positive")

        path = self._repository_path(
            owner,
            repository,
            f"pulls/{pull_request_number}/files",
        )
        page = 1
        page_size = min(100, limit + 1)
        files: list[ChangedFile] = []
        while True:
            response = await self._request(
                "GET",
                path,
                params={"per_page": page_size, "page": page},
            )
            payload = self._json_list(response)
            for item in payload:
                files.append(self._changed_file_from_payload(item))
                if len(files) > limit:
                    raise TooManyChangedFilesError(len(files), limit)

            link_header = response.headers.get("Link", "")
            has_next_link = 'rel="next"' in link_header
            if len(payload) < page_size or not has_next_link:
                break
            page += 1
        return files

    async def get_pull_request_diff(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        max_chars: int | None = None,
        truncate: bool = True,
    ) -> PullRequestDiff:
        """Fetch a unified diff and bound it to a character budget."""

        self._validate_pull_number(pull_request_number)
        limit = self.max_diff_chars if max_chars is None else max_chars
        if limit <= 0:
            raise ValueError("max_chars must be positive")
        path = self._repository_path(
            owner,
            repository,
            f"pulls/{pull_request_number}",
        )
        response = await self._request(
            "GET",
            path,
            accept=GITHUB_DIFF_MEDIA_TYPE,
            max_response_bytes=(limit + 1) * 4,
            truncate_response=True,
        )
        content = response.content.decode("utf-8", errors="replace")
        response_truncated = bool(
            response.extensions.get("pullsage_body_truncated", False)
        )
        original_length = (
            max(len(content) + 1, limit + 1)
            if response_truncated
            else len(content)
        )
        if original_length <= limit:
            return PullRequestDiff(
                content=content,
                original_length=original_length,
                truncated=False,
                max_chars=limit,
            )
        if not truncate:
            raise PullRequestTooLargeError(
                resource="unified diff",
                actual=original_length,
                limit=limit,
            )

        bounded = content[:limit]
        last_newline = bounded.rfind("\n")
        if last_newline >= int(limit * 0.8):
            bounded = bounded[: last_newline + 1]
        return PullRequestDiff(
            content=bounded,
            original_length=original_length,
            truncated=True,
            max_chars=limit,
        )

    @staticmethod
    def _posted_review_from_payload(payload: Mapping[str, Any]) -> PostedReview:
        try:
            return PostedReview(
                id=payload["id"],
                state=payload["state"],
                html_url=payload.get("html_url"),
                body=payload.get("body"),
                submitted_at=GitHubClient._parse_datetime(payload.get("submitted_at")),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise GitHubAPIError(
                "GitHub review response failed schema validation.",
                safe_message="GitHub returned an invalid review response.",
            ) from exc

    async def post_pull_request_review(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        body: str,
        event: ReviewEvent | str = ReviewEvent.COMMENT,
        comments: Sequence[GitHubReviewComment | Mapping[str, Any]] = (),
        commit_id: str | None = None,
    ) -> PostedReview:
        """Create one review containing a summary and valid inline comments."""

        self._validate_pull_number(pull_request_number)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("review body must be non-empty")
        try:
            resolved_event = event if isinstance(event, ReviewEvent) else ReviewEvent(event)
            resolved_comments = [
                item
                if isinstance(item, GitHubReviewComment)
                else GitHubReviewComment.model_validate(item)
                for item in comments
            ]
        except (ValueError, ValidationError) as exc:
            raise ValueError("review event or inline comments are invalid") from exc

        path = self._repository_path(
            owner,
            repository,
            f"pulls/{pull_request_number}/reviews",
        )
        payload: dict[str, Any] = {
            "body": body.strip(),
            "event": resolved_event.value,
        }
        if commit_id is not None:
            if (
                not isinstance(commit_id, str)
                or not commit_id.strip()
                or len(commit_id.strip()) > 128
            ):
                raise ValueError("commit_id must be a non-empty commit SHA")
            payload["commit_id"] = commit_id.strip()
        if resolved_comments:
            payload["comments"] = [comment.to_api_payload() for comment in resolved_comments]
        response = await self._request(
            "POST",
            path,
            json_payload=payload,
        )
        return self._posted_review_from_payload(self._json_object(response))

    async def post_review(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        body: str,
        event: ReviewEvent | str = ReviewEvent.COMMENT,
        comments: Sequence[GitHubReviewComment | Mapping[str, Any]] = (),
        commit_id: str | None = None,
    ) -> PostedReview:
        """Compatibility alias for :meth:`post_pull_request_review`."""

        return await self.post_pull_request_review(
            owner,
            repository,
            pull_request_number,
            body=body,
            event=event,
            comments=comments,
            commit_id=commit_id,
        )

    # Read aliases make tool wiring explicit without duplicating behavior.
    get_pull_request_files = get_changed_files
    get_diff = get_pull_request_diff
