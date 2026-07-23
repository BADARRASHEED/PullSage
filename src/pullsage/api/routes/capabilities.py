"""Safe, non-secret PullSage capability reporting."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from pullsage.ai.codex_runner import CodexRunner
from pullsage.api.dependencies import (
    get_codex_runner,
    get_settings,
    setting_value,
)
from pullsage.api.routes.health import _codex_available
from pullsage.config import Settings

router = APIRouter(prefix="/api/v1/config", tags=["configuration"])


class CapabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posting_enabled: bool
    codex_available: bool
    mcp_write_tools_enabled: bool
    max_diff_chars: int = Field(ge=1)
    max_changed_files: int = Field(ge=1)
    max_concurrent_reviews: int = Field(ge=1)
    in_memory_jobs: bool = True


@router.get(
    "/capabilities",
    response_model=CapabilityResponse,
    summary="Get safe runtime capabilities",
)
async def capabilities(
    settings: Settings = Depends(get_settings),
    codex_runner: CodexRunner = Depends(get_codex_runner),
) -> CapabilityResponse:
    """Return feature switches and bounded limits, never secret material."""

    return CapabilityResponse(
        posting_enabled=bool(
            setting_value(
                settings,
                "post_comments",
                prefixed_name="pullsage_post_comments",
                default=False,
            )
        ),
        codex_available=await _codex_available(codex_runner, settings),
        mcp_write_tools_enabled=bool(
            setting_value(
                settings,
                "allow_mcp_write_tools",
                prefixed_name="pullsage_allow_mcp_write_tools",
                default=False,
            )
        ),
        max_diff_chars=int(
            setting_value(
                settings,
                "max_diff_chars",
                prefixed_name="pullsage_max_diff_chars",
                default=200_000,
            )
        ),
        max_changed_files=int(
            setting_value(
                settings,
                "max_changed_files",
                prefixed_name="pullsage_max_changed_files",
                default=100,
            )
        ),
        max_concurrent_reviews=int(
            setting_value(
                settings,
                "max_concurrent_reviews",
                prefixed_name="pullsage_max_concurrent_reviews",
                default=2,
            )
        ),
    )
