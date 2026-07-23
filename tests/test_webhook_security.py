"""Unit tests for GitHub webhook authenticity and delivery deduplication."""

from __future__ import annotations

import pytest

from pullsage.exceptions import WebhookSignatureError
from pullsage.github.webhook_security import (
    DeliveryCache,
    compute_webhook_signature,
    is_supported_pull_request_action,
    should_process_pull_request,
    verify_signature,
    verify_webhook_signature,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_valid_webhook_signature() -> None:
    body = b'{"action":"opened"}'
    signature = compute_webhook_signature(body, "test-secret")

    assert verify_signature(body, signature, "test-secret") is True
    assert verify_webhook_signature(body, signature, "test-secret") is None


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "sha1=deadbeef",
        "sha256=not-hex",
        f"sha256={'0' * 64}",
    ],
)
def test_invalid_or_missing_webhook_signature(
    signature: str | None,
) -> None:
    body = b'{"action":"opened"}'

    assert verify_signature(body, signature, "test-secret") is False
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(body, signature, "test-secret")


@pytest.mark.parametrize(
    ("action", "supported"),
    [
        ("opened", True),
        ("reopened", True),
        ("synchronize", True),
        ("ready_for_review", True),
        ("closed", False),
        ("edited", False),
        (None, False),
    ],
)
def test_supported_pull_request_actions(
    action: str | None,
    supported: bool,
) -> None:
    assert is_supported_pull_request_action(action) is supported


def test_drafts_wait_until_ready_for_review() -> None:
    assert should_process_pull_request("pull_request", "opened", True) is False
    assert (
        should_process_pull_request(
            "pull_request",
            "ready_for_review",
            True,
        )
        is True
    )
    assert should_process_pull_request("issues", "opened", False) is False


def test_delivery_cache_deduplicates_expires_and_stays_bounded() -> None:
    clock = _Clock()
    cache = DeliveryCache(max_entries=2, ttl_seconds=10, clock=clock)

    assert cache.check_and_store("delivery-1") is True
    assert cache.check_and_store("delivery-1") is False
    assert cache.check_and_store("delivery-2") is True
    assert len(cache) == 2

    assert cache.check_and_store("delivery-3") is True
    assert len(cache) == 2
    assert cache.contains("delivery-1") is False

    clock.now += 11
    assert len(cache) == 0
    assert cache.check_and_store("delivery-3") is True
