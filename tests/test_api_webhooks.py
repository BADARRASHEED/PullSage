"""GitHub webhook verification, routing, and delivery deduplication."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi.testclient import TestClient

from pullsage.api.app import create_app
from pullsage.api.dependencies import ServiceBundle
from pullsage.config import Settings

_SECRET = "webhook-test-secret"


class _FakeGitHubClient:
    async def aclose(self) -> None:
        return None


class _FakeCodexRunner:
    def is_available(self) -> bool:
        return True


class _FakeReviewService:
    async def review_pull_request(self, *args: object, **kwargs: object) -> None:
        return None


def _service_factory(_settings: Settings) -> ServiceBundle:
    return ServiceBundle(
        github_client=_FakeGitHubClient(),  # type: ignore[arg-type]
        codex_runner=_FakeCodexRunner(),  # type: ignore[arg-type]
        review_service=_FakeReviewService(),  # type: ignore[arg-type]
    )


def _payload(action: str = "opened", *, draft: bool = False) -> dict[str, Any]:
    return {
        "action": action,
        "number": 12,
        "repository": {
            "name": "example",
            "owner": {"login": "octo-org"},
        },
        "pull_request": {
            "draft": draft,
            "head": {"sha": "a" * 40},
        },
    }


def _signed_request(
    client: TestClient,
    payload: dict[str, Any],
    *,
    delivery: str,
    signature: str | None = None,
):
    body = json.dumps(payload, separators=(",", ":")).encode()
    effective_signature = signature or (
        "sha256="
        + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    )
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": effective_signature,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
        },
    )


def _application():
    return create_app(
        Settings(
            _env_file=None,
            github_token="test-token",
            github_webhook_secret=_SECRET,
        ),
        service_factory=_service_factory,
    )


def test_supported_webhook_is_accepted_and_delivery_is_deduplicated() -> None:
    with TestClient(_application()) as client:
        first = _signed_request(
            client,
            _payload(),
            delivery="delivery-accepted-1",
        )
        duplicate = _signed_request(
            client,
            _payload(),
            delivery="delivery-accepted-1",
        )

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert first.json()["job_id"]
    assert duplicate.status_code == 202
    assert duplicate.json()["status"] == "duplicate"


def test_invalid_signature_is_rejected_before_payload_processing() -> None:
    with TestClient(_application()) as client:
        response = _signed_request(
            client,
            {"not": "a pull request payload"},
            delivery="delivery-invalid-signature",
            signature="sha256=" + ("0" * 64),
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_webhook_signature"


def test_unsupported_action_and_draft_pull_request_are_ignored() -> None:
    with TestClient(_application()) as client:
        unsupported = _signed_request(
            client,
            _payload("closed"),
            delivery="delivery-unsupported-action",
        )
        draft = _signed_request(
            client,
            _payload("opened", draft=True),
            delivery="delivery-draft",
        )

    assert unsupported.status_code == 202
    assert unsupported.json()["status"] == "ignored"
    assert draft.status_code == 202
    assert draft.json()["status"] == "ignored"
