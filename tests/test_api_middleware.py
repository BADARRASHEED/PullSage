"""ASGI-level request bounds and correlation tests without FastAPI."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pullsage.api.middleware import RequestIDMiddleware


def test_declared_oversized_body_is_rejected_before_receive_or_application() -> None:
    called = {"receive": False, "application": False}
    sent: list[dict[str, Any]] = []

    async def application(
        _scope: dict[str, Any],
        _receive: Any,
        _send: Any,
    ) -> None:
        called["application"] = True

    async def receive() -> dict[str, Any]:
        called["receive"] = True
        raise AssertionError("oversized declared body must not be consumed")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"content-length", b"1048577"),
            (b"x-request-id", b"bounded-request"),
        ],
    }

    asyncio.run(RequestIDMiddleware(application)(scope, receive, send))

    assert called == {"receive": False, "application": False}
    assert sent[0]["status"] == 413
    assert (b"x-request-id", b"bounded-request") in sent[0]["headers"]
    payload = json.loads(sent[1]["body"])
    assert payload["error"]["code"] == "request_body_too_large"
    assert payload["error"]["request_id"] == "bounded-request"


def test_bounded_body_is_replayed_and_response_gets_request_id() -> None:
    sent: list[dict[str, Any]] = []
    received_body: list[bytes] = []
    incoming = [
        {
            "type": "http.request",
            "body": b'{"ok":',
            "more_body": True,
        },
        {
            "type": "http.request",
            "body": b"true}",
            "more_body": False,
        },
    ]

    async def receive() -> dict[str, Any]:
        return incoming.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def application(
        _scope: dict[str, Any],
        replay_receive: Any,
        response_send: Any,
    ) -> None:
        while True:
            message = await replay_receive()
            received_body.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await response_send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await response_send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"x-request-id", b"replayed-request")],
    }

    asyncio.run(RequestIDMiddleware(application)(scope, receive, send))

    assert b"".join(received_body) == b'{"ok":true}'
    assert sent[0]["status"] == 204
    assert (b"x-request-id", b"replayed-request") in sent[0]["headers"]
