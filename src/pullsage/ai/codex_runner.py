"""Safe, asynchronous invocation of the local Codex CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import suppress
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
_MAX_RESULT_BYTES = (_MAX_RESULT_CHARS * 4) + 4
_MAX_DIAGNOSTIC_BYTES = 262_144
_PROCESS_REAP_TIMEOUT_SECONDS = 5.0
_CODEX_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        # Operating-system process discovery and user configuration locations.
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        # TLS and proxy configuration needed to reach the Codex service.
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        # Codex/OpenAI configuration. PullSage and GitHub secrets are excluded.
        "CODEX_API_KEY",
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
    }
)


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
            stdout_task = asyncio.create_task(
                self._read_stream_bounded(process.stdout, _MAX_RESULT_BYTES),
                name="pullsage-codex-stdout",
            )
            stderr_task = asyncio.create_task(
                self._read_stream_bounded(process.stderr, _MAX_DIAGNOSTIC_BYTES),
                name="pullsage-codex-stderr",
            )
            stdout_capture, stderr_capture = await asyncio.wait_for(
                self._wait_for_process(
                    process,
                    prompt.encode("utf-8"),
                    stdout_task,
                    stderr_task,
                ),
                timeout=self._settings.codex_timeout_seconds,
            )
        except TimeoutError as error:
            stdout_task.cancel()
            stderr_task.cancel()
            await self._terminate_process(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise CodexTimeoutError(self._settings.codex_timeout_seconds) from error
        except asyncio.CancelledError:
            stdout_task.cancel()
            stderr_task.cancel()
            await self._terminate_process(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        duration_ms = round((time.monotonic() - started) * 1_000)
        stdout_bytes, stdout_truncated = stdout_capture
        stderr_bytes, stderr_truncated = stderr_capture
        execution = _CodexExecution(
            stdout=self._decode_capture(stdout_bytes, stdout_truncated),
            stderr=self._decode_capture(stderr_bytes, stderr_truncated),
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
            "-c",
            'shell_environment_policy.inherit="none"',
        ]
        if self._settings.codex_model:
            command.extend(("--model", self._settings.codex_model))
        command.append("-")
        return command

    @staticmethod
    async def _read_stream_bounded(
        stream: asyncio.StreamReader | None,
        max_bytes: int,
    ) -> tuple[bytes, bool]:
        """Drain a subprocess pipe while retaining only a bounded prefix."""

        if stream is None:
            return b"", False
        retained = bytearray()
        truncated = False
        while chunk := await stream.read(65_536):
            remaining = max_bytes - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                truncated = True
        return bytes(retained), truncated

    @staticmethod
    async def _write_stdin(
        process: asyncio.subprocess.Process,
        prompt: bytes,
    ) -> None:
        writer = process.stdin
        if writer is None:
            return
        try:
            writer.write(prompt)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()

    @classmethod
    async def _wait_for_process(
        cls,
        process: asyncio.subprocess.Process,
        prompt: bytes,
        stdout_task: asyncio.Task[tuple[bytes, bool]],
        stderr_task: asyncio.Task[tuple[bytes, bool]],
    ) -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
        await cls._write_stdin(process, prompt)
        await process.wait()
        stdout_capture, stderr_capture = await asyncio.gather(
            stdout_task,
            stderr_task,
        )
        return stdout_capture, stderr_capture

    @staticmethod
    def _decode_capture(value: bytes, truncated: bool) -> str:
        decoded = value.decode("utf-8", errors="replace")
        if truncated:
            return f"{decoded}\n[PullSage capture truncated]"
        return decoded

    @staticmethod
    def _subprocess_environment() -> dict[str, str]:
        """Pass only OS/Codex essentials, never GitHub or PullSage secrets."""

        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _CODEX_ENVIRONMENT_ALLOWLIST or key.upper().startswith("LC_")
        }
        environment.update(
            {
                "NO_COLOR": "1",
                "TERM": "dumb",
            }
        )
        return environment

    @staticmethod
    async def _terminate_process(
        process: asyncio.subprocess.Process,
    ) -> None:
        """Stop and reap a timed-out or cancelled direct Codex child."""

        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_PROCESS_REAP_TIMEOUT_SECONDS,
            )
        except (ProcessLookupError, TimeoutError):
            logger.warning(
                "Codex child process could not be reaped within the safety timeout",
                extra={
                    "event": "codex_process_reap_timeout",
                    "status": "degraded",
                },
            )

    @staticmethod
    def _serialize_context(context: PullRequestContext) -> dict[str, Any]:
        if hasattr(context, "model_dump"):
            value = context.model_dump(mode="json")
            if isinstance(value, dict):
                changed_files = value.get("changed_files")
                if isinstance(changed_files, list):
                    for changed_file in changed_files:
                        if isinstance(changed_file, dict):
                            # These navigational URLs are irrelevant to a
                            # review and can carry repository-specific data.
                            changed_file.pop("blob_url", None)
                            changed_file.pop("raw_url", None)
                            changed_file.pop("contents_url", None)
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
            if result_path.stat().st_size > _MAX_RESULT_BYTES:
                raise InvalidCodexOutputError(
                    "Codex review output exceeded the allowed result size."
                )
            with result_path.open("r", encoding="utf-8") as result_file:
                raw_value = result_file.read(_MAX_RESULT_CHARS + 1)
            if len(raw_value) > _MAX_RESULT_CHARS:
                raise InvalidCodexOutputError(
                    "Codex review output exceeded the allowed result size."
                )
            value = raw_value.strip()
        else:
            value = stdout.strip()
        if not value:
            raise InvalidCodexOutputError("Codex completed without a review result.")
        if len(value) > _MAX_RESULT_CHARS:
            raise InvalidCodexOutputError("Codex review output exceeded the allowed result size.")
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
