"""Phantom token + HMAC verification middleware (mandatory on every request)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse

from commandclaw_mcp.auth.hmac_verify import HMACVerificationError, HMACVerifier
from commandclaw_mcp.auth.phantom import TokenStore
from commandclaw_mcp.observability.metrics import validation_failures_total

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger()

# Headers used for phantom token + HMAC
HEADER_PHANTOM_TOKEN = "x-phantom-token"
HEADER_TIMESTAMP = "x-timestamp"
HEADER_SIGNATURE = "x-signature"
HEADER_NONCE = "x-nonce"

# Paths that skip auth (health checks + session creation)
_PUBLIC_PATHS = frozenset({"/health", "/ready", "/metrics", "/sessions"})

# MCP protocol headers to preserve (not strip)
HEADER_MCP_SESSION_ID = "mcp-session-id"
HEADER_MCP_PROTOCOL_VERSION = "mcp-protocol-version"


class AuthMiddleware:
    """ASGI middleware: validates phantom token + HMAC on every request.

    Steps:
    1. Extract headers (X-Phantom-Token, X-Timestamp, X-Signature, X-Nonce)
    2. Look up phantom token in current generation, fall back to previous
    3. Verify HMAC signature (timestamp freshness, nonce uniqueness, constant-time compare)
    4. Attach PhantomSession to request state for downstream use
    5. Strip phantom/HMAC headers before forwarding
    """

    def __init__(
        self,
        app: ASGIApp,
        token_store: TokenStore,
        hmac_verifier: HMACVerifier,
    ) -> None:
        self.app = app
        self._token_store = token_store
        self._hmac_verifier = hmac_verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        # Skip auth for public endpoints
        if request.url.path in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        # Extract required headers
        phantom_token = request.headers.get(HEADER_PHANTOM_TOKEN)
        timestamp = request.headers.get(HEADER_TIMESTAMP)
        signature = request.headers.get(HEADER_SIGNATURE)
        nonce = request.headers.get(HEADER_NONCE)

        if not all([phantom_token, timestamp, signature, nonce]):
            validation_failures_total.labels(reason="unknown_token").inc()
            response = JSONResponse(
                {"error": "Missing required auth headers"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        # Look up phantom token (current gen, fall back to previous)
        session = self._token_store.lookup(phantom_token)  # type: ignore[arg-type]
        if session is None:
            validation_failures_total.labels(reason="unknown_token").inc()
            logger.warning("auth_unknown_token")
            response = JSONResponse(
                {"error": "Unknown or expired token"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        # Read body for HMAC verification
        body = await request.body()

        # Verify HMAC signature
        try:
            self._hmac_verifier.verify(
                method=request.method,
                path=request.url.path,
                timestamp=timestamp,  # type: ignore[arg-type]
                nonce=nonce,  # type: ignore[arg-type]
                body=body,
                signature=signature,  # type: ignore[arg-type]
                hmac_key=session.hmac_key,
            )
        except HMACVerificationError as exc:
            logger.warning("auth_hmac_failed", reason=exc.reason, agent_id=session.agent_id)
            response = JSONResponse(
                {"error": f"HMAC verification failed: {exc.reason}"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        # Attach session to request state for downstream middleware/handlers
        scope.setdefault("state", {})
        scope["state"]["phantom_session"] = session
        scope["state"]["agent_id"] = session.agent_id

        # Preserve Mcp-Session-Id for upstream propagation (MCP spec requirement)
        mcp_session_id = request.headers.get(HEADER_MCP_SESSION_ID)
        if mcp_session_id:
            scope["state"]["mcp_session_id"] = mcp_session_id

        # Strip phantom/HMAC auth headers from the scope before forwarding
        # Preserve MCP protocol headers (Mcp-Session-Id, MCP-Protocol-Version)
        _auth_headers_to_strip = {
            HEADER_PHANTOM_TOKEN, HEADER_TIMESTAMP, HEADER_SIGNATURE, HEADER_NONCE
        }
        filtered_headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.decode("latin-1").lower() not in _auth_headers_to_strip
        ]
        scope["headers"] = filtered_headers

        await self.app(scope, receive, send)
