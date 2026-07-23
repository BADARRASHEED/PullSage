"""GitHub webhook authenticity, routing, and replay protection helpers."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from pullsage.exceptions import WebhookSignatureError

GITHUB_SIGNATURE_PREFIX = "sha256="
SUPPORTED_GITHUB_EVENT = "pull_request"
SUPPORTED_PULL_REQUEST_ACTIONS = frozenset(
    {"opened", "reopened", "synchronize", "ready_for_review"}
)
_SIGNATURE_PATTERN = re.compile(r"^sha256=[0-9a-fA-F]{64}$")


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, bytes):
        return secret
    if isinstance(secret, str):
        return secret.encode("utf-8")
    raise TypeError("webhook secret must be text or bytes")


def compute_webhook_signature(body: bytes, secret: str | bytes) -> str:
    """Compute the value GitHub sends in ``X-Hub-Signature-256``."""

    if not isinstance(body, bytes):
        raise TypeError("webhook body must be raw bytes")
    key = _secret_bytes(secret)
    if not key:
        raise ValueError("webhook secret cannot be empty")
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    return f"{GITHUB_SIGNATURE_PREFIX}{digest}"


def verify_signature(
    body: bytes,
    signature: str | None,
    secret: str | bytes,
) -> bool:
    """Return whether a raw body has a valid GitHub HMAC-SHA256 signature."""

    if not isinstance(body, bytes):
        raise TypeError("webhook body must be raw bytes")
    if not signature or not _SIGNATURE_PATTERN.fullmatch(signature.strip()):
        return False
    try:
        expected = compute_webhook_signature(body, secret)
    except (TypeError, ValueError):
        return False
    # Hexadecimal is case-insensitive, but GitHub normally sends lowercase.
    return hmac.compare_digest(expected, signature.strip().lower())


def verify_webhook_signature(
    body: bytes,
    signature: str | None,
    secret: str | bytes,
) -> None:
    """Verify a webhook or raise a response-safe domain exception."""

    if not verify_signature(body, signature, secret):
        raise WebhookSignatureError()


def is_supported_pull_request_action(action: str | None) -> bool:
    """Return whether an action should start an automated review."""

    return action in SUPPORTED_PULL_REQUEST_ACTIONS


def should_process_pull_request(
    event: str | None,
    action: str | None,
    draft: bool,
) -> bool:
    """Apply PullSage's event, action, and draft policy."""

    if event != SUPPORTED_GITHUB_EVENT:
        return False
    if not is_supported_pull_request_action(action):
        return False
    return not draft or action == "ready_for_review"


class DeliveryCache:
    """A bounded TTL cache for recently accepted GitHub delivery IDs.

    The cache is deliberately process-local: it prevents accidental repeated
    processing during one process lifetime, not adversarial replay across
    restarts. ``check_and_store`` is atomic and returns ``True`` only for a new
    delivery.
    """

    def __init__(
        self,
        *,
        max_entries: int = 10_000,
        ttl_seconds: float = 3_600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.max_entries = max_entries
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._entries:
            _, expiry = next(iter(self._entries.items()))
            if expiry > now:
                break
            self._entries.popitem(last=False)

    def check_and_store(self, delivery_id: str) -> bool:
        """Atomically store a delivery, returning ``False`` for a duplicate."""

        if not isinstance(delivery_id, str) or not delivery_id.strip():
            raise ValueError("delivery_id must be a non-empty string")
        normalized = delivery_id.strip()
        now = self._clock()
        with self._lock:
            self._prune(now)
            expiry = self._entries.get(normalized)
            if expiry is not None and expiry > now:
                return False
            self._entries.pop(normalized, None)
            while len(self._entries) >= self.max_entries:
                self._entries.popitem(last=False)
            self._entries[normalized] = now + self.ttl_seconds
            return True

    def contains(self, delivery_id: str) -> bool:
        """Return whether a delivery is currently retained without storing it."""

        if not isinstance(delivery_id, str) or not delivery_id.strip():
            return False
        normalized = delivery_id.strip()
        now = self._clock()
        with self._lock:
            self._prune(now)
            expiry = self._entries.get(normalized)
            return expiry is not None and expiry > now

    def discard(self, delivery_id: str) -> None:
        """Remove a delivery, primarily for failed enqueue rollback."""

        with self._lock:
            self._entries.pop(delivery_id.strip(), None)

    def clear(self) -> None:
        """Remove all cached deliveries."""

        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return len(self._entries)


# Explicit name useful in dependency annotations.
WebhookDeliveryCache = DeliveryCache
