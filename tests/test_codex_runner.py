"""Offline tests for Codex command safety and structured-output repair."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from pullsage.ai.codex_runner import CodexRunner, _CodexExecution
from pullsage.config import Settings
from pullsage.exceptions import (
    CodexNotFoundError,
    InvalidCodexOutputError,
)


class _FakeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_full_name: str = "octo/example"
    pull_request_number: int = 7
    unified_diff: str = "diff --git a/a.py b/a.py"


def _valid_review_json() -> str:
    return json.dumps(
        {
            "summary": "No supported defects were found in the supplied change.",
            "verdict": "comment",
            "confidence": 0.9,
            "risk_level": "low",
            "findings": [],
            "testing_recommendations": [],
            "limitations": ["Tests were not executed."],
        }
    )


@pytest.mark.asyncio
async def test_missing_codex_executable_fails_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pullsage.ai.codex_runner.shutil.which", lambda _command: None)
    runner = CodexRunner(Settings(codex_command="missing-codex", _env_file=None))

    with pytest.raises(CodexNotFoundError, match="Codex CLI is not installed"):
        await runner.review(_FakeContext())  # type: ignore[arg-type]


def test_codex_command_is_ephemeral_read_only_and_noninteractive(tmp_path: Path) -> None:
    runner = CodexRunner(Settings(_env_file=None))
    command = runner._build_command(
        executable="codex",
        workspace=tmp_path,
        schema_path=tmp_path / "schema.json",
        result_path=tmp_path / "result.json",
    )

    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "mcp_servers={}" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--yolo" not in command
    assert command[-1] == "-"


@pytest.mark.asyncio
async def test_invalid_output_gets_exactly_one_repair_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pullsage.ai.codex_runner.shutil.which",
        lambda _command: "codex",
    )
    runner = CodexRunner(Settings(_env_file=None))
    outputs = iter(("{not-json", _valid_review_json()))
    prompts: list[str] = []

    async def fake_execute(**kwargs: Any) -> _CodexExecution:
        prompts.append(str(kwargs["prompt"]))
        return _CodexExecution(
            stdout=next(outputs),
            stderr="",
            return_code=0,
            duration_ms=1,
        )

    monkeypatch.setattr(runner, "_execute", fake_execute)
    result = await runner.review(_FakeContext())  # type: ignore[arg-type]

    assert result.findings == []
    assert len(prompts) == 2
    assert "Validation errors:" in prompts[1]


@pytest.mark.asyncio
async def test_second_invalid_output_fails_without_unbounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pullsage.ai.codex_runner.shutil.which",
        lambda _command: "codex",
    )
    runner = CodexRunner(Settings(_env_file=None))
    calls = 0

    async def fake_execute(**_kwargs: Any) -> _CodexExecution:
        nonlocal calls
        calls += 1
        return _CodexExecution(
            stdout="not-json",
            stderr="",
            return_code=0,
            duration_ms=1,
        )

    monkeypatch.setattr(runner, "_execute", fake_execute)
    with pytest.raises(InvalidCodexOutputError):
        await runner.review(_FakeContext())  # type: ignore[arg-type]

    assert calls == 2
