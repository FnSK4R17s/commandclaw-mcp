"""Structured request/response logging middleware."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from starlette.requests import Request
from starlette.responses import Response

from commandclaw_mcp.observability.metrics import (
    gateway_request_duration_seconds,
    gateway_requests_total,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger()


class LoggingMiddleware:
    """ASGI middleware: structured request/response logging with metrics.

    Logs: method, path, status, duration, agent_id (from upstream auth middleware).
    Does NOT log: request bodies, query parameters, tokens, credentials.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        start_time = time.monotonic()
        status_code = 500  # Default in case of unhandled exception

        async def send_wrapper(message: dict) -> None:  # type: ignore[type-arg]
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.monotonic() - start_time
            agent_id = scope.get("state", {}).get("agent_id", "anonymous")
            tool = request.url.path

            # Prometheus metrics
            gateway_requests_total.labels(
                agent_id=agent_id, tool=tool, status=str(status_code)
            ).inc()
            gateway_request_duration_seconds.labels(
                agent_id=agent_id, tool=tool
            ).observe(duration)

            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_ms=round(duration * 1000, 2),
                agent_id=agent_id,
            )
