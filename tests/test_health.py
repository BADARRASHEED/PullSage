"""API liveness and degraded-readiness behavior."""

from fastapi.testclient import TestClient

from pullsage.api.app import create_app
from pullsage.api.dependencies import ServiceBundle
from pullsage.config import Settings


class _FakeGitHubClient:
    async def aclose(self) -> None:
        return None


class _FakeCodexRunner:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


class _FakeReviewService:
    async def review_pull_request(self, *args: object, **kwargs: object) -> None:
        return None


def _factory(available: bool):
    def build(_settings: Settings) -> ServiceBundle:
        return ServiceBundle(
            github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
            codex_runner=_FakeCodexRunner(available),  # type: ignore[arg-type]
            review_service=_FakeReviewService(),  # type: ignore[arg-type]
        )

    return build


def test_health_is_live_and_propagates_request_id() -> None:
    application = create_app(
        Settings(_env_file=None),
        service_factory=_factory(available=False),
    )
    with TestClient(application) as client:
        response = client.get(
            "/health",
            headers={"X-Request-ID": "health-test-123"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "pullsage"}
    assert response.headers["X-Request-ID"] == "health-test-123"


def test_readiness_reports_each_missing_requirement() -> None:
    application = create_app(
        Settings(_env_file=None),
        service_factory=_factory(available=False),
    )
    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "settings_loaded": True,
            "worker_running": True,
            "github_token_configured": False,
            "webhook_secret_configured": False,
            "codex_available": False,
        },
    }


def test_readiness_is_ready_when_all_requirements_are_present() -> None:
    settings = Settings(
        _env_file=None,
        github_token="test-token",
        github_webhook_secret="test-secret",
    )
    application = create_app(
        settings,
        service_factory=_factory(available=True),
    )
    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert all(response.json()["checks"].values())


def test_capabilities_expose_only_safe_limits_and_switches() -> None:
    settings = Settings(
        _env_file=None,
        post_comments=True,
        allow_mcp_write_tools=True,
        max_diff_chars=12_345,
    )
    application = create_app(
        settings,
        service_factory=_factory(available=True),
    )
    with TestClient(application) as client:
        response = client.get("/api/v1/config/capabilities")

    assert response.status_code == 200
    assert response.json()["posting_enabled"] is True
    assert response.json()["mcp_write_tools_enabled"] is True
    assert response.json()["max_diff_chars"] == 12_345
    assert "github_token" not in response.text.casefold()
    assert "webhook_secret" not in response.text.casefold()
