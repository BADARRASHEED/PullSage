"""Offline tests for Codex command safety and structured-output repair."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from pullsage.ai.codex_runner import CodexRunner, _CodexExecution
from pullsage.config import Settings
from pullsage.exceptions import (
    CodexNotFoundError,
    CodexRuntimeError,
    CodexTimeoutError,
    InvalidCodexOutputError,
)


class _FakeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_full_name: str = "octo/example"
    pull_request_number: int = 7
    unified_diff: str = "diff --git a/a.py b/a.py"


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.killed = False
        self.stdin = _FakeWriter()
        self.stdout = _HangingReader()
        self.stderr = _HangingReader()
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        self.started.set()
        await self._exited.wait()
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()


class _CompletedProcess:
    def __init__(self, *, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdin = _FakeWriter()
        self.stdout = _BytesReader(b"")
        self.stderr = _BytesReader(stderr)

    async def wait(self) -> int:
        return self.returncode


class _FakeWriter:
    def write(self, _value: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _BytesReader:
    def __init__(self, value: bytes) -> None:
        self._value = value

    async def read(self, _size: int) -> bytes:
        value, self._value = self._value, b""
        return value


class _HangingReader:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""


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
    assert 'shell_environment_policy.inherit="none"' in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--yolo" not in command
    assert command[-1] == "-"


def test_codex_environment_excludes_github_and_pullsage_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-codex")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "must-not-reach-codex")
    monkeypatch.setenv("PULLSAGE_INTERNAL_SECRET", "must-not-reach-codex")
    monkeypatch.setenv("OPENAI_API_KEY", "codex-auth")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    environment = CodexRunner._subprocess_environment()

    assert environment["OPENAI_API_KEY"] == "codex-auth"
    assert "GITHUB_TOKEN" not in environment
    assert "GITHUB_WEBHOOK_SECRET" not in environment
    assert "PULLSAGE_INTERNAL_SECRET" not in environment


def test_timeout_kills_and_reaps_codex_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _HangingProcess()

    async def create_process(*_args: object, **_kwargs: object) -> _HangingProcess:
        return process

    monkeypatch.setattr(
        "pullsage.ai.codex_runner.asyncio.create_subprocess_exec",
        create_process,
    )
    runner = CodexRunner(Settings(codex_timeout_seconds=0.01, _env_file=None))

    async def scenario() -> None:
        with pytest.raises(CodexTimeoutError):
            await runner._execute(
                executable="codex",
                workspace=tmp_path,
                schema_path=tmp_path / "schema.json",
                result_path=tmp_path / "result.json",
                prompt="review",
            )

    asyncio.run(scenario())
    assert process.killed is True


def test_cancellation_kills_and_reaps_codex_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _HangingProcess()

    async def create_process(*_args: object, **_kwargs: object) -> _HangingProcess:
        return process

    monkeypatch.setattr(
        "pullsage.ai.codex_runner.asyncio.create_subprocess_exec",
        create_process,
    )
    runner = CodexRunner(Settings(_env_file=None))

    async def scenario() -> None:
        task = asyncio.create_task(
            runner._execute(
                executable="codex",
                workspace=tmp_path,
                schema_path=tmp_path / "schema.json",
                result_path=tmp_path / "result.json",
                prompt="review",
            )
        )
        await process.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.killed is True


def test_nonzero_codex_exit_maps_to_typed_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _CompletedProcess(
        returncode=1,
        stderr=b"authentication required",
    )

    async def create_process(*_args: object, **_kwargs: object) -> _CompletedProcess:
        return process

    monkeypatch.setattr(
        "pullsage.ai.codex_runner.asyncio.create_subprocess_exec",
        create_process,
    )
    runner = CodexRunner(Settings(_env_file=None))

    async def scenario() -> None:
        with pytest.raises(CodexRuntimeError, match="not authenticated"):
            await runner._execute(
                executable="codex",
                workspace=tmp_path,
                schema_path=tmp_path / "schema.json",
                result_path=tmp_path / "result.json",
                prompt="review",
            )

    asyncio.run(scenario())


def test_result_file_takes_priority_over_progress_stdout(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(_valid_review_json(), encoding="utf-8")

    candidate = CodexRunner._read_candidate(
        result_path,
        "diagnostic progress, not JSON",
    )

    assert candidate == _valid_review_json()


def test_result_file_is_rejected_before_unbounded_read(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    with result_path.open("wb") as result_file:
        result_file.seek(8_000_004)
        result_file.write(b"x")

    with pytest.raises(InvalidCodexOutputError, match="allowed result size"):
        CodexRunner._read_candidate(result_path, "")


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
