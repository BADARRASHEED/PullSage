"""PullSage HTTP API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pullsage.api.app import app, create_app

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    """Load the FastAPI application only when a caller requests it."""

    if name == "app":
        from pullsage.api.app import app

        return app
    if name == "create_app":
        from pullsage.api.app import create_app

        return create_app
    raise AttributeError(name)
