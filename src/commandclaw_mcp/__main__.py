"""Entry point: load config, create app, run uvicorn."""

from __future__ import annotations

import sys

import uvicorn

from commandclaw_mcp.config import load_settings
from commandclaw_mcp.gateway.app import create_app
from commandclaw_mcp.security.validation import ConfigValidationError


def main() -> None:
    """Load settings, validate, and start the gateway."""
    settings = load_settings()

    try:
        app = create_app(settings)
    except ConfigValidationError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)  # noqa: T201 — startup only
        sys.exit(1)

    uvicorn.run(
        app,
        host=settings.gateway.host,
        port=settings.gateway.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
