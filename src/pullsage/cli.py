"""Console launchers for the API and MCP processes."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from pullsage.config import get_settings
from pullsage.logging_config import configure_logging


def run_api() -> None:
    """Start the PullSage FastAPI process."""

    from pullsage.api.app import create_app

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


def run_mcp() -> None:
    """Start the PullSage MCP server over STDIO."""

    from pullsage.mcp.server import main as run

    settings = get_settings()
    configure_logging(settings.log_level)
    run()


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch the convenience ``pullsage`` command."""

    parser = argparse.ArgumentParser(
        prog="pullsage",
        description="PullSage AI pull-request review services",
    )
    parser.add_argument(
        "service",
        nargs="?",
        choices=("api", "mcp"),
        default="api",
        help="service to start (default: api)",
    )
    args = parser.parse_args(argv)
    if args.service == "mcp":
        run_mcp()
    else:
        run_api()
