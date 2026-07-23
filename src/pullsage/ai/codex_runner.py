"""Safe, asynchronous invocation of the local Codex CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pullsage.ai.output_schema import (
    format_validation_error,
    parse_review_output,
    review_json_schema,
)
from pullsage.ai.prompt_builder import build_repair_prompt, build_review_prompt
from pullsage.config import Settings
from pullsage.exceptions import (
    CodexNotFoundError,
    CodexRuntimeError,
    CodexTimeoutError,
    InvalidCodexOutputError,
)
from pullsage.reviews.models import PullRequestContext, ReviewResult

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 2_000_000


@dataclass(frozen=True, slots=True)
class _CodexExecution:
    """Captured process outcome; content is intentionally never logged."""

    stdout: str
    stderr: str
    return_code: int
    duration_ms: int


class CodexRunner:
    """Run Codex in an ephemeral, read-only workspace and validate its result."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def command_path(self) -> str | None:
        """Resolve the configured executable without invoking a shell."""

        return shutil.which(self._settings.codex_command)

    def is_available(self) -> bool:
        """Return whether the configured Codex executable can be resolved."""

        return self.command_path is not None

    async def review(self, context: PullRequestContext) -> ReviewResult:
        """Review a bounded PR context, with one repair attempt for invalid JSON."""

        executable = self.command_path
        if executable is None:
            raise CodexNotFoundError(self._settings.codex_command)

        with tempfile.TemporaryDirectory(prefix="pullsage-review-") as temporary:
            workspace = Path(temporary)
            context_path = workspace / "pr_context.json"
            schema_path = workspace / "review_schema.json"
            result_path = workspace / "review_result.json"

            self._write_json(context_path, self._serialize_context(context))
            self._write_json(schema_path, review_json_schema())

            prompt = build_review_prompt(
                context_filename=context_path.name,
                schema_filename=schema_path.name,
            )
            first = await self._execute(
                executable=executable,
                workspace=workspace,
                schema_path=schema_path,
                result_path=result_path,
                prompt=prompt,
            )
            try:
                candidate = self._read_candidate(result_path, first.stdout)
                return parse_review_output(candidate)
            except (InvalidCodexOutputError, TypeError, ValueError) as first_error:
                validation_errors = format_validation_error(first_error)
                logger.warning(
                    "Codex returned invalid structured output; attempting one repair",
                    extra={
                        "event": "codex_output_repair",
                        "duration_ms": first.duration_ms,
                        "status": "retrying",
                    },
                )

            result_path.unlink(missing_ok=True)
            repair_prompt = build_repair_prompt(
                validation_errors,
                context_filename=context_path.name,
                schema_filename=schema_path.name,
            )
            second = await self._execute(
                executable=executable,
                workspace=workspace,
                schema_path=schema_path,
                result_path=result_path,
                prompt=repair_prompt,
            )
            try:
                repaired_candidate = self._read_candidate(result_path, second.stdout)
                return parse_review_output(repaired_candidate)
            except (InvalidCodexOutputError, TypeError, ValueError) as second_error:
                details = format_validation_error(second_error)
                raise InvalidCodexOutputError(
                    "Codex returned invalid structured review output after one "
                    f"repair attempt. Validation details: {details}"
                ) from second_error

    async def run_review(self, context: PullRequestContext) -> ReviewResult:
        """Compatibility alias for service-layer dependency injection."""

        return await self.review(context)

    async def _execute(
        self,
        *,
        executable: str,
        workspace: Path,
        schema_path: Path,
        result_path: Path,
        prompt: str,
    ) -> _CodexExecution:
        command = self._build_command(
            executable=executable,
            workspace=workspace,
            schema_path=schema_path,
            result_path=result_path,
        )
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=self._subprocess_environment(),
            )
        except OSError as error:
            raise CodexRuntimeError(
                "Codex CLI could not be started. Check CODEX_COMMAND and local "
                "execution permissions."
            ) from error

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self._settings.codex_timeout_seconds,
            )
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise CodexTimeoutError(self._settings.codex_timeout_seconds) from error

        duration_ms = round((time.monotonic() - started) * 1_000)
        execution = _CodexExecution(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            return_code=process.returncode or 0,
            duration_ms=duration_ms,
        )
        logger.info(
            "Codex execution finished",
            extra={
                "event": "codex_execution_finished",
                "duration_ms": duration_ms,
                "status": "succeeded" if execution.return_code == 0 else "failed",
            },
        )
        if execution.return_code != 0:
            self._raise_runtime_error(execution)
        return execution

    def _build_command(
        self,
        *,
        executable: str,
        workspace: Path,
        schema_path: Path,
        result_path: Path,
    ) -> list[str]:
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "--cd",
            str(workspace),
            "-c",
            "mcp_servers={}",
        ]
        if self._settings.codex_model:
            command.extend(("--model", self._settings.codex_model))
        command.append("-")
        return command

    @staticmethod
    def _subprocess_environment() -> dict[str, str]:
        """Inherit authentication while suppressing interactive terminal behavior."""

        environment = os.environ.copy()
        environment.update(
            {
                "NO_COLOR": "1",
                "TERM": "dumb",
            }
        )
        return environment

    @staticmethod
    def _serialize_context(context: PullRequestContext) -> dict[str, Any]:
        if hasattr(context, "model_dump"):
            value = context.model_dump(mode="json")
            if isinstance(value, dict):
                return value
        raise TypeError("Pull-request context must be a Pydantic model")

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    @staticmethod
    def _read_candidate(result_path: Path, stdout: str) -> str:
        if result_path.is_file():
            value = result_path.read_text(encoding="utf-8").strip()
        else:
            value = stdout.strip()
        if not value:
            raise InvalidCodexOutputError("Codex completed without a review result.")
        if len(value) > _MAX_RESULT_CHARS:
            raise InvalidCodexOutputError(
                "Codex review output exceeded the allowed result size."
            )
        return value

    @staticmethod
    def _raise_runtime_error(execution: _CodexExecution) -> None:
        diagnostics = execution.stderr.casefold()
        if any(
            marker in diagnostics
            for marker in ("not logged in", "not authenticated", "authentication required")
        ):
            message = (
                "Codex CLI is not authenticated. Authenticate the local Codex "
                "installation and retry."
            )
        else:
            message = (
                "Codex CLI could not complete the review "
                f"(exit code {execution.return_code}). Check local Codex diagnostics."
            )
        raise CodexRuntimeError(message)
