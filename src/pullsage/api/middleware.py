"""Small ASGI middleware used by the PullSage API."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from pullsage.logging_config import (
    reset_request_id,
    set_request_id,
)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


class RequestIDMiddleware:
    """Propagate a safe correlation ID for each HTTP request."""

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
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)
