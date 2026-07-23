"""Small ASGI middleware used by the PullSage API."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from pullsage.logging_config import (
    reset_request_id,
    set_request_id,
)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_MAX_REQUEST_BODY_BYTES = 1_048_576
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class RequestIDMiddleware:
    """Propagate a correlation ID and bound mutating request bodies."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for key, value in scope.get("headers", ()):
            if key.lower() == b"x-request-id":
                incoming = value.decode("ascii", errors="ignore")
                break
        request_id = (
            incoming
            if incoming is not None and _VALID_REQUEST_ID.fullmatch(incoming)
            else uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = request_id
        token = set_request_id(request_id)

        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", ())
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            bounded_receive = receive
            if scope.get("method", "").upper() in _BODY_METHODS:
                messages: list[dict[str, Any]] = []
                body_size = 0
                too_large = False
                while True:
                    message = await receive()
                    messages.append(message)
                    if message["type"] == "http.disconnect":
                        break
                    if message["type"] != "http.request":
                        continue
                    body_size += len(message.get("body", b""))
                    if body_size > _MAX_REQUEST_BODY_BYTES:
                        too_large = True
                        break
                    if not message.get("more_body", False):
                        break

                if too_large:
                    payload = json.dumps(
                        {
                            "error": {
                                "code": "request_body_too_large",
                                "message": "Request body exceeds the 1 MiB limit.",
                                "request_id": request_id,
                            }
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                    await send_with_request_id(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode("ascii")),
                            ],
                        }
                    )
                    await send_with_request_id(
                        {
                            "type": "http.response.body",
                            "body": payload,
                            "more_body": False,
                        }
                    )
                    return

                message_index = 0

                async def replay_receive() -> dict[str, Any]:
                    nonlocal message_index
                    if message_index < len(messages):
                        message = messages[message_index]
                        message_index += 1
                        return message
                    return await receive()

                bounded_receive = replay_receive

            await self.app(scope, bounded_receive, send_with_request_id)
        finally:
            reset_request_id(token)
