"""Unit tests for the async GitHub REST client with an in-memory transport."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from pullsage.exceptions import (
    GitHubAPIError,
    GitHubAuthenticationError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from pullsage.github.client import GitHubClient
from pullsage.github.models import (
    GitHubReviewComment,
    ReviewCommentSide,
    ReviewEvent,
)


def _pull_request_payload() -> dict[str, object]:
    return {
        "number": 7,
        "title": "Handle empty input",
        "body": "Adds input handling.",
        "state": "open",
        "draft": False,
        "html_url": "https://github.example/octo/example/pull/7",
        "user": {"login": "octocat"},
        "base": {"ref": "main"},
        "head": {"ref": "feature", "sha": "abc123"},
        "additions": 4,
        "deletions": 1,
        "changed_files": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }


def _changed_file_payload() -> dict[str, object]:
    return {
        "filename": "src/example.py",
        "status": "modified",
        "additions": 4,
        "deletions": 1,
        "changes": 5,
        "sha": "def456",
        "patch": "@@ -1 +1 @@\n-old\n+new\n",
    }


def test_fetches_metadata_files_and_bounded_diff() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[_changed_file_payload()])
        if request.headers["Accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text="0123456789abcdefghij")
        return httpx.Response(200, json=_pull_request_payload())

    async def scenario() -> tuple[object, list[object], object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = GitHubClient(
                "test-token",
                "https://api.github.test",
                max_diff_chars=10,
                client=http_client,
            )
            pull_request = await client.get_pull_request("octo", "example", 7)
            files = await client.get_changed_files("octo", "example", 7)
            diff = await client.get_pull_request_diff("octo", "example", 7)
            return pull_request, files, diff

    pull_request, files, diff = asyncio.run(scenario())

    assert pull_request.head_sha == "abc123"
    assert files[0].filename == "src/example.py"
    assert diff.content == "0123456789"
    assert diff.original_length == 20
    assert diff.truncated is True
    assert len(requests) == 3


@pytest.mark.parametrize(
    ("status_code", "headers", "body", "exception_type"),
    [
        (401, {}, {"message": "Bad credentials"}, GitHubAuthenticationError),
        (404, {}, {"message": "Not Found"}, GitHubNotFoundError),
        (
            403,
            {"X-RateLimit-Remaining": "0", "Retry-After": "12"},
            {"message": "API rate limit exceeded"},
            GitHubRateLimitError,
        ),
    ],
)
def test_maps_github_api_errors(
    status_code: int,
    headers: dict[str, str],
    body: dict[str, str],
    exception_type: type[Exception],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers=headers,
            json=body,
            request=request,
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = GitHubClient("test-token", client=http_client)
            await client.get_pull_request("octo", "example", 7)

    with pytest.raises(exception_type):
        asyncio.run(scenario())


def test_posts_one_review_with_inline_comments() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": 123,
                "state": "COMMENTED",
                "html_url": "https://github.example/review/123",
                "body": "review",
                "submitted_at": "2026-01-02T00:00:00Z",
            },
            request=request,
        )

    async def scenario() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = GitHubClient("test-token", client=http_client)
            return await client.post_pull_request_review(
                "octo",
                "example",
                7,
                body="Review summary",
                event=ReviewEvent.COMMENT,
                commit_id="abc123",
                comments=[
                    GitHubReviewComment(
                        path="src/example.py",
                        body="Check the empty-input case.",
                        line=2,
                        side=ReviewCommentSide.RIGHT,
                    )
                ],
            )

    posted = asyncio.run(scenario())

    assert posted.id == 123
    assert captured["event"] == "COMMENT"
    assert captured["commit_id"] == "abc123"
    assert len(captured["comments"]) == 1  # type: ignore[arg-type]


def test_changed_files_follow_github_pagination() -> None:
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested_pages.append(page)
        if page == 1:
            payload = [
                {
                    **_changed_file_payload(),
                    "filename": f"src/file_{index}.py",
                }
                for index in range(100)
            ]
            return httpx.Response(
                200,
                json=payload,
                headers={
                    "Link": (
                        '<https://api.github.test/files?page=2>; rel="next", '
                        '<https://api.github.test/files?page=2>; rel="last"'
                    )
                },
                request=request,
            )
        return httpx.Response(
            200,
            json=[
                {
                    **_changed_file_payload(),
                    "filename": "src/final.py",
                }
            ],
            request=request,
        )

    async def scenario() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = GitHubClient(
                "test-token",
                max_changed_files=150,
                client=http_client,
            )
            return await client.get_changed_files("octo", "example", 7)

    files = asyncio.run(scenario())

    assert len(files) == 101
    assert requested_pages == [1, 2]


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_timeout_and_generic_server_error_map_safely(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            500,
            json={"message": "internal details"},
            request=request,
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = GitHubClient("test-token", client=http_client)
            await client.get_pull_request("octo", "example", 7)

    with pytest.raises(GitHubAPIError):
        asyncio.run(scenario())
