"""Shared review-service orchestration and dry-run safety."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from pullsage.config import Settings
from pullsage.exceptions import StalePullRequestHeadError
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
from pullsage.reviews.models import PullRequestContext, ReviewResult
from pullsage.reviews.service import ReviewService


class _FakeGitHubClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.head_sha = "abc123"
        self.changed_file = ChangedFile(
            filename="src/example.py",
            status=ChangedFileStatus.MODIFIED,
            additions=1,
            deletions=1,
            changes=2,
            patch="@@ -1 +1 @@\n-old = 1\n+new = 2\n",
        )

    async def get_pull_request(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
    ) -> PullRequest:
        return PullRequest(
            repository_full_name=f"{owner}/{repository}",
            number=pull_request_number,
            title="Update the value",
            body="A small change.",
            state=PullRequestState.OPEN,
            draft=False,
            html_url="https://github.example/octo/example/pull/7",
            author_login="octocat",
            base_ref="main",
            head_ref="feature",
            head_sha=self.head_sha,
            additions=1,
            deletions=1,
            changed_files=1,
        )

    async def get_changed_files(
        self,
        _owner: str,
        _repository: str,
        _pull_request_number: int,
        *,
        max_files: int | None = None,
    ) -> list[ChangedFile]:
        assert max_files == 100
        return [self.changed_file]

    async def get_pull_request_diff(
        self,
        _owner: str,
        _repository: str,
        _pull_request_number: int,
        *,
        max_chars: int | None = None,
        truncate: bool = True,
    ) -> PullRequestDiff:
        assert max_chars == 200_000
        assert truncate is True
        content = "diff --git a/src/example.py b/src/example.py\n"
        return PullRequestDiff(
            content=content,
            original_length=len(content),
            truncated=False,
            max_chars=max_chars,
        )

    async def post_pull_request_review(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        body: str,
        event: ReviewEvent | str,
        comments: Sequence[GitHubReviewComment],
        commit_id: str | None = None,
    ) -> PostedReview:
        self.posted.append(
            {
                "owner": owner,
                "repository": repository,
                "pull_request_number": pull_request_number,
                "body": body,
                "event": event,
                "comments": list(comments),
                "commit_id": commit_id,
            }
        )
        return PostedReview(id=1, state="COMMENTED")


class _FakeCodexRunner:
    async def review(self, context: PullRequestContext) -> ReviewResult:
        assert context.repository_full_name == "octo/example"
        return ReviewResult.model_validate(
            {
                "summary": "No supported defects were found.",
                "verdict": "comment",
                "confidence": 0.9,
                "risk_level": "low",
                "findings": [],
                "testing_recommendations": [],
                "limitations": ["Tests were not executed."],
            }
        )


class _HeadChangingCodexRunner(_FakeCodexRunner):
    def __init__(self, github: _FakeGitHubClient) -> None:
        self.github = github

    async def review(self, context: PullRequestContext) -> ReviewResult:
        result = await super().review(context)
        self.github.head_sha = "def456"
        return result


def test_review_is_dry_run_by_default_and_reports_real_progress() -> None:
    github = _FakeGitHubClient()
    service = ReviewService(
        Settings(_env_file=None),
        github,  # type: ignore[arg-type]
        _FakeCodexRunner(),
    )
    phases: list[str] = []

    async def scenario() -> ReviewResult:
        async def progress(phase: str) -> None:
            phases.append(phase)

        return await service.review_pull_request(
            "octo",
            "example",
            7,
            progress_callback=progress,
        )

    review = asyncio.run(scenario())

    assert review.findings == []
    assert phases == ["reviewing", "validating"]
    assert github.posted == []


def test_explicit_post_uses_one_validated_github_review() -> None:
    github = _FakeGitHubClient()
    service = ReviewService(
        Settings(_env_file=None),
        github,  # type: ignore[arg-type]
        _FakeCodexRunner(),
    )
    phases: list[str] = []

    async def scenario() -> None:
        async def progress(phase: str) -> None:
            phases.append(phase)

        await service.review_pull_request(
            "octo",
            "example",
            7,
            post_comments=True,
            progress_callback=progress,
        )

    asyncio.run(scenario())

    assert phases == ["reviewing", "validating", "posting"]
    assert len(github.posted) == 1
    assert github.posted[0]["event"] is ReviewEvent.COMMENT
    assert github.posted[0]["commit_id"] == "abc123"
    assert "## PullSage review" in github.posted[0]["body"]


def test_head_change_during_codex_run_rejects_stale_result() -> None:
    github = _FakeGitHubClient()
    service = ReviewService(
        Settings(_env_file=None),
        github,  # type: ignore[arg-type]
        _HeadChangingCodexRunner(github),
    )

    with pytest.raises(StalePullRequestHeadError):
        asyncio.run(service.review_pull_request("octo", "example", 7))

    assert github.posted == []


def test_context_limitations_reserve_space_and_remain_schema_valid() -> None:
    payload = _FakeCodexRunner()
    review = asyncio.run(
        payload.review(
            PullRequestContext(
                repository_full_name="octo/example",
                pull_request_number=7,
                title="Change",
                author_login="octocat",
                base_branch="main",
                head_branch="feature",
                head_sha="abc123",
                body=None,
                state="open",
                additions=1,
                deletions=1,
                changed_files=[],
                unified_diff="",
                diff_original_length=0,
                warnings=[],
            )
        )
    )
    review_payload = review.model_dump(mode="python")
    review_payload["limitations"] = [f"Model limitation {index}." for index in range(20)]
    full_review = ReviewResult.model_validate(review_payload)

    bounded = ReviewService._add_context_limitations(
        full_review,
        ["Diff truncated.", "Patches truncated.", "Body truncated."],
    )

    assert len(bounded.limitations) == 20
    assert bounded.limitations[-3:] == [
        "Diff truncated.",
        "Patches truncated.",
        "Body truncated.",
    ]
    ReviewResult.model_validate(bounded.model_dump(mode="python"))
