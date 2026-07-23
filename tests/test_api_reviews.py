"""Manual review API behavior without GitHub or Codex calls."""

from collections.abc import Awaitable, Callable
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pullsage.api.app import create_app
from pullsage.api.dependencies import ServiceBundle
from pullsage.config import Settings


class _FakeGitHubClient:
    async def get_pull_request(
        self,
        _owner: str,
        _repository: str,
        _pull_request_number: int,
    ) -> SimpleNamespace:
        return SimpleNamespace(head_sha="manual-head-sha")

    async def aclose(self) -> None:
        return None


class _FakeCodexRunner:
    def is_available(self) -> bool:
        return True


class _FakeReviewService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, bool]] = []

    async def review_pull_request(
        self,
        owner: str,
        repository: str,
        pull_request_number: int,
        *,
        post_comments: bool = False,
        progress_callback: Callable[[str], Awaitable[None]] | None = None,
        expected_head_sha: str | None = None,
    ) -> None:
        assert expected_head_sha == "manual-head-sha"
        self.calls.append((owner, repository, pull_request_number, post_comments))
        if progress_callback is not None:
            await progress_callback("reviewing")
            await progress_callback("validating")
            if post_comments:
                await progress_callback("posting")
        return None


def test_manual_review_is_queued_and_can_be_looked_up() -> None:
    review_service = _FakeReviewService()

    def service_factory(_settings: Settings) -> ServiceBundle:
        return ServiceBundle(
            github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
            codex_runner=_FakeCodexRunner(),  # type: ignore[arg-type]
            review_service=review_service,  # type: ignore[arg-type]
        )

    application = create_app(
        Settings(_env_file=None),
        service_factory=service_factory,
    )
    with TestClient(application) as client:
        accepted = client.post(
            "/api/v1/reviews",
            json={
                "owner": "octo-org",
                "repository": "example",
                "pull_request_number": 42,
                "post_comments": False,
            },
        )
        fetched = client.get(f"/api/v1/reviews/{accepted.json()['job_id']}")

    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"
    assert accepted.json()["deduplicated"] is False
    assert fetched.status_code == 200
    assert fetched.json()["owner"] == "octo-org"
    assert fetched.json()["repository"] == "example"
    assert fetched.json()["pull_request_number"] == 42
    assert fetched.json()["source"] == "manual"
    assert review_service.calls == [("octo-org", "example", 42, False)]


def test_manual_review_validation_uses_consistent_error_envelope() -> None:
    def service_factory(_settings: Settings) -> ServiceBundle:
        return ServiceBundle(
            github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
            codex_runner=_FakeCodexRunner(),  # type: ignore[arg-type]
            review_service=_FakeReviewService(),  # type: ignore[arg-type]
        )

    application = create_app(
        Settings(_env_file=None),
        service_factory=service_factory,
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/reviews",
            json={
                "owner": "octo-org",
                "repository": "example",
                "pull_request_number": 0,
                "unexpected": True,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["request_id"]


def test_unknown_review_job_returns_safe_404() -> None:
    def service_factory(_settings: Settings) -> ServiceBundle:
        return ServiceBundle(
            github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
            codex_runner=_FakeCodexRunner(),  # type: ignore[arg-type]
            review_service=_FakeReviewService(),  # type: ignore[arg-type]
        )

    application = create_app(
        Settings(_env_file=None),
        service_factory=service_factory,
    )
    with TestClient(application) as client:
        response = client.get("/api/v1/reviews/41ce2b73-b01f-41c9-9e6f-120269071e04")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_manual_posting_requires_explicit_true_request_value() -> None:
    review_service = _FakeReviewService()

    def service_factory(_settings: Settings) -> ServiceBundle:
        return ServiceBundle(
            github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
            codex_runner=_FakeCodexRunner(),  # type: ignore[arg-type]
            review_service=review_service,  # type: ignore[arg-type]
        )

    application = create_app(
        Settings(_env_file=None, post_comments=False),
        service_factory=service_factory,
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/reviews",
            json={
                "owner": "octo-org",
                "repository": "example",
                "pull_request_number": 43,
                "post_comments": True,
            },
        )

    assert response.status_code == 202
    assert review_service.calls == [("octo-org", "example", 43, True)]


def test_manual_posting_rejects_coerced_boolean_values() -> None:
    def service_factory(_settings: Settings) -> ServiceBundle:
        return ServiceBundle(
            github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
            codex_runner=_FakeCodexRunner(),  # type: ignore[arg-type]
            review_service=_FakeReviewService(),  # type: ignore[arg-type]
        )

    application = create_app(
        Settings(_env_file=None),
        service_factory=service_factory,
    )
    with TestClient(application) as client:
        for invalid_value in (1, "yes", "on"):
            response = client.post(
                "/api/v1/reviews",
                json={
                    "owner": "octo-org",
                    "repository": "example",
                    "pull_request_number": 43,
                    "post_comments": invalid_value,
                },
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_error"


def test_manual_request_body_is_bounded_before_json_parsing() -> None:
    def service_factory(_settings: Settings) -> ServiceBundle:
        return ServiceBundle(
            github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
            codex_runner=_FakeCodexRunner(),  # type: ignore[arg-type]
            review_service=_FakeReviewService(),  # type: ignore[arg-type]
        )

    application = create_app(
        Settings(_env_file=None),
        service_factory=service_factory,
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/reviews",
            content=b"x" * 1_048_577,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"
    assert response.headers["x-request-id"]
