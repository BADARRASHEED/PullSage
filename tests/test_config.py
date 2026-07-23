"""Configuration defaults and secret handling."""

from pydantic import SecretStr

from pullsage.config import Settings


def test_settings_have_write_safe_defaults() -> None:
    settings = Settings(
        github_token=None,
        github_webhook_secret=None,
        _env_file=None,
    )

    assert settings.post_comments is False
    assert settings.allow_mcp_write_tools is False
    assert settings.min_confidence == 0.8
    assert settings.github_api_url == "https://api.github.com"


def test_settings_accept_field_names_and_protect_secrets() -> None:
    settings = Settings(
        github_token="github-secret-value",
        github_webhook_secret="webhook-secret-value",
        _env_file=None,
    )

    assert isinstance(settings.github_token, SecretStr)
    assert settings.github_token_value() == "github-secret-value"
    assert "github-secret-value" not in repr(settings)


def test_empty_secret_values_are_unconfigured() -> None:
    settings = Settings(
        github_token=" ",
        github_webhook_secret="",
        _env_file=None,
    )

    assert settings.github_token is None
    assert settings.github_webhook_secret is None
