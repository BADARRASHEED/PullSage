"""MCP adapter tests for default read safety and explicit write gating."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from pullsage.config import Settings
from pullsage.mcp.server import PullSageMCPTools
from pullsage.reviews.models import ReviewResult


def _review() -> ReviewResult:
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


class _Dumpable:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.value


def _tools(
    *,
    allow_writes: bool,
) -> tuple[PullSageMCPTools, AsyncMock, AsyncMock]:
    github_client = AsyncMock()
    review_service = AsyncMock()
    adapter = PullSageMCPTools(
        Settings(allow_mcp_write_tools=allow_writes, _env_file=None),
        github_client=github_client,
        codex_runner=AsyncMock(),
        review_service=review_service,
    )
    return adapter, github_client, review_service


@pytest.mark.asyncio
async def test_review_posting_parameter_is_blocked_by_default() -> None:
    adapter, _github, review_service = _tools(allow_writes=False)

    response = await adapter.review_pull_request(
        "octo",
        "example",
        7,
        post_comments=True,
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "mcp_write_tools_disabled"
    review_service.review_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_post_tool_is_blocked_by_default() -> None:
    adapter, _github, review_service = _tools(allow_writes=False)

    response = await adapter.post_review("octo", "example", 7, _review())

    assert response["ok"] is False
    assert response["error"]["code"] == "mcp_write_tools_disabled"
    review_service.post_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_review_returns_validated_result() -> None:
    adapter, _github, review_service = _tools(allow_writes=False)
    review_service.review_pull_request.return_value = _review()

    response = await adapter.review_pull_request("octo", "example", 7)

    assert response["ok"] is True
    assert response["posted"] is False
    assert response["review"]["findings"] == []
    review_service.review_pull_request.assert_awaited_once_with(
        "octo",
        "example",
        7,
        post_comments=False,
    )


@pytest.mark.asyncio
async def test_enabled_post_still_requires_structured_review_payload() -> None:
    adapter, _github, review_service = _tools(allow_writes=True)

    response = await adapter.post_review(
        "octo",
        "example",
        7,
        {"summary": "arbitrary text"},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_review_payload"
    review_service.post_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_post_uses_shared_review_service() -> None:
    adapter, _github, review_service = _tools(allow_writes=True)
    review_service.post_review.return_value = _Dumpable({"id": 123, "state": "COMMENTED"})

    response = await adapter.post_review("octo", "example", 7, _review())

    assert response == {
        "ok": True,
        "posted_review": {"id": 123, "state": "COMMENTED"},
    }
    review_service.post_review.assert_awaited_once()
