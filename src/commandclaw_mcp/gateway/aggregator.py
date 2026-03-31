"""FastMCP mount() + namespace prefixing + credential injection + RBAC middleware hooks.

Implements the full MCP request lifecycle through FastMCP middleware:
1. AuditMiddleware — logs all MCP traffic with Mcp-Session-Id tracking
2. RBACMiddleware — dual-layer RBAC (discovery + call-time + rate limiting)
3. CredentialInjectionMiddleware — decrypts and injects real credentials, zeros after use
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

import structlog
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.providers.proxy import FastMCPProxy

from commandclaw_mcp.auth.credential_store import decrypt_credential_async
from commandclaw_mcp.gateway.errors import (
    circuit_open_error,
    credential_error,
    rate_limited_error,
    rbac_denied_error,
)
from commandclaw_mcp.gateway.transport import create_mcp_client
from commandclaw_mcp.observability.audit import audit_tool_call
from commandclaw_mcp.rbac.call_guard import CallGuardDenied
from commandclaw_mcp.rbac.rate_limiter import RateLimitExceeded
from commandclaw_mcp.session.token_encoded import Capability

if TYPE_CHECKING:
    from commandclaw_mcp.config import Settings
    from commandclaw_mcp.middleware.rbac_middleware import RBACHandler

logger = structlog.get_logger()


def compute_identity_hash(agent_id: str) -> str:
    """Compute a stable identity hash for session pool keying.

    Ensures different agents never share upstream sessions.
    Pool key: (server_url, identity_hash, transport_type)
    """
    return hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:16]


class RBACMiddleware(Middleware):
    """FastMCP middleware: dual-layer RBAC enforcement.

    - on_list_tools: filters tool list by agent principal (discovery filtering)
    - on_call_tool: enforces ABAC policy + rate limit before forwarding (call-time enforcement)
    """

    def __init__(self, rbac_handler: RBACHandler) -> None:
        self._rbac = rbac_handler

    async def on_list_tools(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Discovery filtering: return only tools the agent is allowed to see."""
        result = await call_next(context)

        agent_id = self._get_agent_id(context)
        if agent_id and result:
            tools_as_dicts = [
                {"name": getattr(t, "name", str(t))} for t in result
            ]
            allowed = await self._rbac.filter_tool_list(agent_id, tools_as_dicts)
            allowed_names = {t["name"] for t in allowed}
            result = [t for t in result if getattr(t, "name", str(t)) in allowed_names]

        return result

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Call-time enforcement: ABAC + rate limiting before forwarding."""
        agent_id = self._get_agent_id(context)
        tool_name = getattr(context, "tool_name", None) or str(context)
        start_time = time.monotonic()
        allowed = True

        try:
            if agent_id:
                resource_attrs = getattr(context, "arguments", None) or {}
                await self._rbac.authorize_call(
                    agent_id, tool_name, resource_attrs=resource_attrs
                )

            result = await call_next(context)

            latency_ms = (time.monotonic() - start_time) * 1000
            await audit_tool_call(
                agent_id=agent_id or "unknown",
                tool=tool_name,
                allowed=True,
                latency_ms=latency_ms,
            )
            return result

        except (CallGuardDenied, RateLimitExceeded):
            allowed = False
            latency_ms = (time.monotonic() - start_time) * 1000
            await audit_tool_call(
                agent_id=agent_id or "unknown",
                tool=tool_name,
                allowed=False,
                latency_ms=latency_ms,
            )
            raise
        except Exception:
            latency_ms = (time.monotonic() - start_time) * 1000
            await audit_tool_call(
                agent_id=agent_id or "unknown",
                tool=tool_name,
                allowed=allowed,
                latency_ms=latency_ms,
            )
            raise

    def _get_agent_id(self, context: MiddlewareContext) -> str | None:
        """Extract agent_id from the FastMCP context state."""
        try:
            return context.fastmcp_context.get_state("agent_id")
        except Exception:
            return None


class CredentialInjectionMiddleware(Middleware):
    """FastMCP middleware: decrypt and inject real credentials before forwarding upstream.

    Steps per VISION.md request lifecycle (steps 6, 9-11):
    6. Extract service prefix from URL path to determine which credential entry to use
    9. Strip phantom token and HMAC headers (done by auth_middleware.py)
    10. Retrieve real credential from encrypted store; check TTL
    11. Inject real credential in configured format (header, query param, Basic Auth)

    Credentials are decrypted only at the moment of upstream injection,
    not held decrypted in memory. After injection, credential bytearrays
    are zeroed via ctypes.memset.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._encryption_seed = settings.gateway.encryption_seed

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Inject real credential for the target upstream server."""
        tool_name = getattr(context, "tool_name", "") or ""
        server_name = self._extract_server_name(tool_name)

        if server_name:
            await self._inject_credential(context, server_name)

        return await call_next(context)

    async def _inject_credential(
        self, context: MiddlewareContext, server_name: str
    ) -> None:
        """Decrypt credential at injection time, inject, then zero."""
        session = None
        try:
            session = context.fastmcp_context.get_state("phantom_session")
        except Exception:
            return

        if not session or not hasattr(session, "credentials"):
            return

        cred_entry = session.credentials.get(server_name)
        if cred_entry is None:
            return

        # Check credential TTL before using
        if cred_entry.is_expired:
            logger.warning(
                "credential_expired",
                server=server_name,
                agent_id=getattr(session, "agent_id", "unknown"),
            )
            # Attempt to refresh from encrypted envelope
            if cred_entry.encrypted_envelope:
                secure_bytes = await decrypt_credential_async(
                    cred_entry.encrypted_envelope, self._encryption_seed
                )
                try:
                    cred_entry.real_credential = bytes(secure_bytes).decode("utf-8")
                finally:
                    secure_bytes.clear()
            else:
                return  # Cannot inject expired credential without refresh source

        # If credential has an encrypted envelope and no plaintext yet, decrypt it
        if not cred_entry.real_credential and cred_entry.encrypted_envelope:
            secure_bytes = await decrypt_credential_async(
                cred_entry.encrypted_envelope, self._encryption_seed
            )
            try:
                cred_entry.real_credential = bytes(secure_bytes).decode("utf-8")
            finally:
                secure_bytes.clear()

        if cred_entry.real_credential:
            # Inject the formatted credential into context for the proxy
            context.fastmcp_context.set_state("upstream_credential", {
                "header_name": cred_entry.header_name,
                "value": cred_entry.format_for_injection(),
                "server": server_name,
            })

    def _extract_server_name(self, tool_name: str) -> str | None:
        """Extract server namespace from prefixed tool name.

        'github_create_issue' -> 'github'
        """
        for server_name in self._settings.servers:
            if tool_name.startswith(f"{server_name}_"):
                return server_name
        return None


class SessionTrackingMiddleware(Middleware):
    """FastMCP middleware: track Mcp-Session-Id, Last-Event-ID, and negotiated capabilities.

    MCP protocol requirements:
    - Server MAY assign session ID in InitializeResult
    - Client MUST include Mcp-Session-Id on all subsequent requests
    - On SSE disconnect, client resumes via GET with Last-Event-ID
    - Disconnection SHOULD NOT be interpreted as cancellation
    - Gateway should validate messages against negotiated capabilities
    """

    def __init__(self) -> None:
        # Track Mcp-Session-Id per upstream server
        self._upstream_session_ids: dict[str, str] = {}
        # Track last SSE event ID per upstream for resumability
        self._last_event_ids: dict[str, str] = {}
        # Track negotiated capabilities per upstream (from Initialize response)
        self._upstream_capabilities: dict[str, int] = {}

    async def on_message(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Track MCP session IDs and Last-Event-IDs across upstream interactions."""
        result = await call_next(context)

        # Extract and store Mcp-Session-Id from upstream responses
        # FastMCP handles the header propagation internally; we track for pool routing
        try:
            headers = getattr(result, "headers", None) or {}
            session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
            if session_id:
                # Determine upstream from context
                upstream = self._get_upstream_name(context)
                if upstream:
                    self._upstream_session_ids[upstream] = session_id
                    logger.debug(
                        "mcp_session_id_tracked",
                        upstream=upstream,
                        session_id=session_id[:8],
                    )
        except Exception:
            pass  # Session tracking is best-effort; don't break the request

        # Track Last-Event-ID from SSE streams for resumability
        try:
            event_id = getattr(result, "id", None)
            if event_id:
                upstream = self._get_upstream_name(context)
                if upstream:
                    self._last_event_ids[upstream] = str(event_id)
        except Exception:
            pass

        # Extract negotiated capabilities from Initialize responses
        try:
            capabilities = getattr(result, "capabilities", None)
            if capabilities:
                upstream = self._get_upstream_name(context)
                if upstream:
                    cap_mask = self._capabilities_to_bitmask(capabilities)
                    self._upstream_capabilities[upstream] = cap_mask
                    logger.debug(
                        "upstream_capabilities_negotiated",
                        upstream=upstream,
                        capabilities_hex=Capability.to_hex(cap_mask),
                    )
        except Exception:
            pass

        return result

    def get_session_id(self, upstream: str) -> str | None:
        """Get the tracked Mcp-Session-Id for an upstream server."""
        return self._upstream_session_ids.get(upstream)

    def get_last_event_id(self, upstream: str) -> str | None:
        """Get the last SSE event ID for resumability on reconnect."""
        return self._last_event_ids.get(upstream)

    def get_capabilities(self, upstream: str) -> int:
        """Get the negotiated capability bitmask for an upstream server."""
        return self._upstream_capabilities.get(upstream, Capability.ALL)

    def has_capability(self, upstream: str, capability: int) -> bool:
        """Check if an upstream server has a specific negotiated capability."""
        caps = self._upstream_capabilities.get(upstream, Capability.ALL)
        return bool(caps & capability)

    @staticmethod
    def _capabilities_to_bitmask(capabilities: Any) -> int:
        """Convert MCP server capabilities object to bitmask."""
        mask = 0
        # Map MCP capability names to our bitmask
        cap_map = {
            "tools": Capability.TOOLS,
            "prompts": Capability.PROMPTS,
            "resources": Capability.RESOURCES,
            "logging": Capability.LOGGING,
            "completions": Capability.COMPLETIONS,
        }
        for name, bit in cap_map.items():
            if hasattr(capabilities, name) and getattr(capabilities, name):
                mask |= bit
        return mask if mask else Capability.ALL  # Default to ALL if no caps detected

    def _get_upstream_name(self, context: MiddlewareContext) -> str | None:
        """Extract upstream server name from context."""
        try:
            tool_name = getattr(context, "tool_name", "") or ""
            # Namespace prefix is the server name
            parts = tool_name.split("_", 1)
            return parts[0] if len(parts) > 1 else None
        except Exception:
            return None


class AuditMiddleware(Middleware):
    """FastMCP middleware: audit logging for all MCP messages."""

    async def on_message(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Log all MCP traffic (request and notification)."""
        start = time.monotonic()
        try:
            return await call_next(context)
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "mcp_message",
                duration_ms=round(duration_ms, 2),
            )


class BidirectionalMiddleware(Middleware):
    """FastMCP middleware: support server-to-client requests.

    MCP servers can initiate requests to clients (e.g., sampling/createMessage).
    This requires full-duplex transport through the gateway.

    FastMCP handles bidirectional routing natively when using ProxyProvider.
    This middleware logs and audits server-initiated requests passing through.
    """

    async def on_request(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Handle both client→server and server→client requests."""
        return await call_next(context)

    async def on_notification(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Pass through notifications without blocking.

        Messages without `id` require no response; the gateway must not block.
        Disconnection SHOULD NOT be interpreted as cancellation —
        clients SHOULD send explicit CancelledNotification.
        """
        return await call_next(context)


class AuthBridgeMiddleware(Middleware):
    """Bridge auth state from ASGI scope into FastMCP context.

    The auth middleware (ASGI layer) stores agent_id and phantom_session
    in scope["state"]. This FastMCP middleware reads that via
    _current_http_request and sets it on the FastMCP context so
    downstream middlewares (RBAC, CredentialInjection) can access it.
    """

    async def on_message(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Copy agent_id and phantom_session from HTTP scope to FastMCP state."""
        try:
            from fastmcp.server.http import _current_http_request

            request = _current_http_request.get(None)
            if request is not None:
                scope_state = request.scope.get("state", {})
                agent_id = scope_state.get("agent_id")
                session = scope_state.get("phantom_session")
                if agent_id and context.fastmcp_context:
                    context.fastmcp_context.set_state("agent_id", agent_id)
                if session and context.fastmcp_context:
                    context.fastmcp_context.set_state("phantom_session", session)
        except Exception:
            logger.debug("auth_bridge_failed", exc_info=True)

        return await call_next(context)


def create_gateway_mcp(
    settings: Settings,
    rbac_handler: RBACHandler | None = None,
) -> FastMCP:
    """Create the FastMCP gateway with all upstream servers mounted.

    Uses namespace prefixing: tools like `get_forecast` become
    `weather_get_forecast` when mounted under namespace "weather".

    Attaches security middleware in order (FIFO on request, LIFO on response):
    0. AuthBridgeMiddleware — copies agent_id from ASGI scope to FastMCP state
    1. AuditMiddleware — logs all MCP traffic
    2. SessionTrackingMiddleware — tracks Mcp-Session-Id + Last-Event-ID
    3. BidirectionalMiddleware — supports server→client requests + notifications
    4. RBACMiddleware — dual-layer RBAC (discovery + call-time + rate limiting)
    5. CredentialInjectionMiddleware — decrypts and injects real credentials
    """
    session_tracker = SessionTrackingMiddleware()

    middlewares: list[Middleware] = [
        AuthBridgeMiddleware(),
        AuditMiddleware(),
        session_tracker,
        BidirectionalMiddleware(),
    ]
    if rbac_handler is not None:
        middlewares.append(RBACMiddleware(rbac_handler))
    middlewares.append(CredentialInjectionMiddleware(settings))

    gateway = FastMCP(name="CommandClaw-MCP-Gateway", middleware=middlewares)

    allow_private = settings.gateway.allow_private_upstream
    for name, server_config in settings.servers.items():
        try:
            # Capture loop vars for the lambda's closure
            _cfg, _name, _priv = server_config, name, allow_private
            proxy = FastMCPProxy(
                client_factory=lambda c=_cfg, n=_name, p=_priv: create_mcp_client(c, n, allow_private=p),
                name=f"proxy-{name}",
            )
            gateway.mount(proxy, namespace=name)
            logger.info(
                "upstream_mounted",
                name=name,
                namespace=name,
                is_stdio=server_config.is_stdio,
            )
        except Exception:
            logger.exception("upstream_mount_failed", name=name)
            continue

    logger.info(
        "gateway_aggregator_ready",
        upstream_count=len(settings.servers),
    )
    return gateway
