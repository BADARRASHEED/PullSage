"""Typed application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """PullSage settings.

    Secret values use :class:`~pydantic.SecretStr` so accidental string
    representations cannot disclose credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
        populate_by_name=True,
    )

    environment: Literal["development", "test", "staging", "production"] = Field(
        default="development",
        validation_alias="PULLSAGE_ENV",
    )
    host: str = Field(default="127.0.0.1", validation_alias="PULLSAGE_HOST")
    port: int = Field(default=8000, ge=1, le=65535, validation_alias="PULLSAGE_PORT")
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = Field(
        default="INFO",
        validation_alias="PULLSAGE_LOG_LEVEL",
    )

    github_token: SecretStr | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    github_webhook_secret: SecretStr | None = Field(
        default=None,
        validation_alias="GITHUB_WEBHOOK_SECRET",
    )
    github_api_url: str = Field(
        default="https://api.github.com",
        validation_alias="GITHUB_API_URL",
    )

    codex_command: str = Field(default="codex", validation_alias="CODEX_COMMAND")
    codex_model: str | None = Field(default=None, validation_alias="CODEX_MODEL")
    codex_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        le=3600,
        validation_alias="CODEX_TIMEOUT_SECONDS",
    )

    post_comments: bool = Field(
        default=False,
        validation_alias="PULLSAGE_POST_COMMENTS",
    )
    allow_mcp_write_tools: bool = Field(
        default=False,
        validation_alias="PULLSAGE_ALLOW_MCP_WRITE_TOOLS",
    )
    min_confidence: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        validation_alias="PULLSAGE_MIN_CONFIDENCE",
    )
    max_diff_chars: int = Field(
        default=200_000,
        ge=1_000,
        validation_alias="PULLSAGE_MAX_DIFF_CHARS",
    )
    max_changed_files: int = Field(
        default=100,
        ge=1,
        le=10_000,
        validation_alias="PULLSAGE_MAX_CHANGED_FILES",
    )
    max_concurrent_reviews: int = Field(
        default=2,
        ge=1,
        le=64,
        validation_alias="PULLSAGE_MAX_CONCURRENT_REVIEWS",
    )
    job_retention_seconds: int = Field(
        default=3_600,
        ge=1,
        validation_alias="PULLSAGE_JOB_RETENTION_SECONDS",
    )
    delivery_retention_seconds: int = Field(
        default=3_600,
        ge=1,
        validation_alias="PULLSAGE_DELIVERY_RETENTION_SECONDS",
    )
    max_webhook_deliveries: int = Field(
        default=10_000,
        ge=100,
        validation_alias="PULLSAGE_MAX_WEBHOOK_DELIVERIES",
    )

    @field_validator("host", "github_api_url", "codex_command")
    @classmethod
    def values_must_not_be_blank(cls, value: str) -> str:
        """Reject configuration that would fail later with an opaque error."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("github_api_url")
    @classmethod
    def normalize_github_api_url(cls, value: str) -> str:
        """Keep URL joining deterministic without accepting non-HTTP schemes."""

        normalized = value.rstrip("/")
        if not normalized.lower().startswith(("https://", "http://")):
            raise ValueError("must use an http or https URL")
        return normalized

    @field_validator("codex_model", mode="before")
    @classmethod
    def empty_model_uses_default(cls, value: object) -> object:
        """Treat an empty environment variable as no explicit model override."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("github_token", "github_webhook_secret", mode="before")
    @classmethod
    def empty_secrets_are_unconfigured(cls, value: object) -> object:
        """Treat empty example-file values as missing credentials."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Accept conventional case-insensitive log-level values."""

        return value.upper() if isinstance(value, str) else value

    def github_token_value(self) -> str | None:
        """Return the GitHub token only at the integration boundary."""

        return self.github_token.get_secret_value() if self.github_token else None

    def webhook_secret_value(self) -> str | None:
        """Return the webhook secret only at the verification boundary."""

        return self.github_webhook_secret.get_secret_value() if self.github_webhook_secret else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached immutable settings object for process-level use."""

    return Settings()


def clear_settings_cache() -> None:
    """Clear process settings, primarily for isolated tests."""

    get_settings.cache_clear()
