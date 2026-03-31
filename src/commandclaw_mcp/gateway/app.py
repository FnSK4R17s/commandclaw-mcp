"""FastAPI app, route registration, lifecycle hooks, health checks."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import ConnectionPool, Redis
from starlette.responses import Response

from commandclaw_mcp.auth.hmac_verify import HMACVerifier
from commandclaw_mcp.auth.phantom import TokenStore, hash_token
from commandclaw_mcp.auth.rotation import RotationManager
from commandclaw_mcp.config import Settings
from commandclaw_mcp.gateway.aggregator import create_gateway_mcp
from commandclaw_mcp.middleware.auth_middleware import AuthMiddleware
from commandclaw_mcp.middleware.logging_middleware import LoggingMiddleware
from commandclaw_mcp.middleware.rbac_middleware import RBACHandler
from commandclaw_mcp.observability.audit import (
    audit_session_event,
    configure_logging,
)
from commandclaw_mcp.observability.tracing import setup_tracing
from commandclaw_mcp.rbac.call_guard import CallGuard
from commandclaw_mcp.rbac.discovery_filter import DiscoveryFilter
from commandclaw_mcp.rbac.policy import PolicyEngine
from commandclaw_mcp.rbac.rate_limiter import RateLimiter
from commandclaw_mcp.security.validation import validate_settings
from commandclaw_mcp.session.manager import SessionManager, SessionMode
from commandclaw_mcp.session.pool import SessionPool
from commandclaw_mcp.session.redis_store import RedisSessionStore
from commandclaw_mcp.session.token_encoded import FallbackEnabledSessionCrypto

logger = structlog.get_logger()


class GatewayState:
    """Holds all shared gateway state for the application lifetime."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token_store = TokenStore()
        self.hmac_verifier = HMACVerifier()
        self.rotation_manager = RotationManager(self.token_store, settings)

        # Redis store (always needed — nonce cache, rate limiter, blocklist)
        self.redis_store = RedisSessionStore(
            settings.redis.url,
            default_ttl=settings.gateway.key_rotation_interval_seconds,
        )

        # Token-encoded crypto (initialized if mode is token_encoded or for future use)
        token_crypto: FallbackEnabledSessionCrypto | None = None
        if settings.gateway.session_mode == "token_encoded":
            token_crypto = FallbackEnabledSessionCrypto(
                primary_seed=settings.gateway.encryption_seed,
                secondary_seed=settings.gateway.encryption_seed_secondary,
            )

        # Unified session manager — switchable between Redis and token-encoded
        session_mode = SessionMode(settings.gateway.session_mode)
        self.session_manager = SessionManager(
            mode=session_mode,
            redis_store=self.redis_store,
            token_crypto=token_crypto,
        )

        self.policy_engine = PolicyEngine(settings.cerbos)
        self.session_pool = SessionPool(
            max_per_key=settings.session_pool.max_per_key,
            ttl_seconds=settings.session_pool.ttl_seconds,
            circuit_breaker_threshold=settings.session_pool.circuit_breaker_threshold,
            circuit_breaker_reset_seconds=settings.session_pool.circuit_breaker_reset_seconds,
        )

        # Redis client for rate limiting (separate pool for rate limiter)
        self._redis_pool = ConnectionPool.from_url(
            settings.redis.url, decode_responses=True
        )
        redis_client = Redis(connection_pool=self._redis_pool)

        self.rate_limiter = RateLimiter(redis_client)

        # Discovery filter with per-session tool count limit (Virtual MCP pattern)
        self.discovery_filter = DiscoveryFilter(
            self.policy_engine,
            max_tools_per_session=settings.gateway.max_tools_per_session,
        )
        self.call_guard = CallGuard(self.policy_engine)

        # Unified RBAC handler wiring all enforcement layers
        self.rbac_handler = RBACHandler(
            discovery_filter=self.discovery_filter,
            call_guard=self.call_guard,
            rate_limiter=self.rate_limiter,
            settings=settings,
        )


def create_app(settings: Settings) -> FastAPI:
    """Create and configure the FastAPI application."""
    # Configure structured logging first
    configure_logging()

    # Validate config at startup — fail fast on dangerous defaults
    validate_settings(settings)

    state = GatewayState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
        """Application lifecycle: startup and shutdown hooks."""
        # Startup
        logger.info(
            "gateway_starting",
            host=settings.gateway.host,
            port=settings.gateway.port,
            session_mode=settings.gateway.session_mode,
        )

        # Initialize OpenTelemetry tracing
        tracer_provider = setup_tracing(settings.observability, app)

        # Connect to Cerbos
        try:
            await state.policy_engine.connect()
        except Exception:
            logger.exception("cerbos_connection_failed")
            # Don't crash — gateway can start, but RBAC will deny-by-default

        # Start key rotation
        await state.rotation_manager.start()

        # Create FastMCP gateway with RBAC middleware hooks wired in
        gateway_mcp = create_gateway_mcp(settings, rbac_handler=state.rbac_handler)
        app.state.gateway_mcp = gateway_mcp

        # Mount FastMCP as sub-application at /mcp
        # Chain its lifespan so the StreamableHTTPSessionManager task group starts
        mcp_asgi = gateway_mcp.http_app(path="/", transport="streamable-http")
        app.mount("/mcp", mcp_asgi)
        logger.info("mcp_protocol_mounted", path="/mcp")

        # Start the MCP sub-app's lifespan (required for task group init)
        mcp_lifespan_cm = mcp_asgi.router.lifespan_context(mcp_asgi)
        await mcp_lifespan_cm.__aenter__()

        # Start periodic pool eviction
        eviction_task = asyncio.create_task(_periodic_eviction(state))

        state._start_time = time.time()
        logger.info("gateway_started")

        try:
            yield {"gateway_state": state}
        finally:
            # Shutdown
            logger.info("gateway_shutting_down")
            eviction_task.cancel()
            try:
                await eviction_task
            except asyncio.CancelledError:
                pass

            # Shutdown MCP sub-app
            await mcp_lifespan_cm.__aexit__(None, None, None)

            await state.rotation_manager.stop()
            await state.redis_store.close()
            await state.policy_engine.close()
            await state._redis_pool.disconnect()

            # Shutdown tracing
            if tracer_provider:
                tracer_provider.shutdown()

        logger.info("gateway_stopped")

    app = FastAPI(
        title="CommandClaw MCP Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Add middleware (order matters: outermost first)
    # CORS → Logging → Auth → app routes
    # Streamable HTTP requires proper CORS configuration per MCP spec
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.gateway.cors_allowed_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Accept",
            "X-Phantom-Token",
            "X-Timestamp",
            "X-Signature",
            "X-Nonce",
            "Mcp-Session-Id",
            "MCP-Protocol-Version",
            "Last-Event-ID",
        ],
        expose_headers=["Mcp-Session-Id"],
    )
    app.add_middleware(AuthMiddleware, token_store=state.token_store, hmac_verifier=state.hmac_verifier)  # type: ignore[arg-type]
    app.add_middleware(LoggingMiddleware)  # type: ignore[arg-type]

    # --- Health & operations routes ---

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe — gateway process is running."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness probe — all dependencies healthy."""
        checks: dict[str, bool] = {}

        # Session manager (covers Redis connectivity)
        checks["session_store"] = await state.session_manager.health_check()

        # Cerbos
        checks["cerbos"] = await state.policy_engine.health_check()

        # Rotation manager
        checks["rotation_manager"] = state.rotation_manager.is_running

        # At least one upstream server
        checks["upstream_servers"] = len(settings.servers) > 0

        all_healthy = all(checks.values())
        status_code = 200 if all_healthy else 503

        return JSONResponse(
            {
                "status": "ready" if all_healthy else "not_ready",
                "checks": checks,
                "session_mode": state.session_manager.mode.value,
            },
            status_code=status_code,
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus metrics endpoint."""
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    # --- Session management routes ---

    @app.post("/sessions")
    async def create_session(request: Request) -> JSONResponse:
        """Create a new phantom session for an agent.

        Body: {"agent_id": "coding-agent"}
        Returns: phantom_token, hmac_key, expires_at
        """
        body = await request.json()
        agent_id = body.get("agent_id")
        if not agent_id:
            return JSONResponse({"error": "agent_id required"}, status_code=400)

        session = state.token_store.create_session_from_config(agent_id, settings)
        if session is None:
            return JSONResponse(
                {"error": f"Unknown agent: {agent_id}"}, status_code=404
            )

        # Store session via unified session manager
        await state.session_manager.store_session(
            session_id=session.phantom_token,
            data={
                "agent_id": session.agent_id,
                "created_at": session.created_at,
                "expires_at": session.expires_at,
            },
            agent_id=session.agent_id,
            ttl=settings.gateway.key_rotation_interval_seconds,
        )

        return JSONResponse(
            {
                "phantom_token": session.phantom_token,
                "hmac_key": session.hmac_key,
                "expires_at": session.expires_at,
                "agent_id": session.agent_id,
            },
            status_code=201,
        )

    @app.get("/token")
    async def get_current_token(request: Request) -> JSONResponse:
        """Polling endpoint: return current token when authenticated with previous.

        For agents surviving across rotation boundaries.
        """
        # Agent must already be authenticated (via auth middleware)
        session = request.scope.get("state", {}).get("phantom_session")
        if session is None:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)

        # Create a new session for this agent
        new_session = state.token_store.create_session_from_config(
            session.agent_id, settings
        )
        if new_session is None:
            return JSONResponse({"error": "Session creation failed"}, status_code=500)

        # Store via session manager
        await state.session_manager.store_session(
            session_id=new_session.phantom_token,
            data={
                "agent_id": new_session.agent_id,
                "created_at": new_session.created_at,
                "expires_at": new_session.expires_at,
            },
            agent_id=new_session.agent_id,
            ttl=settings.gateway.key_rotation_interval_seconds,
        )

        return JSONResponse(
            {
                "phantom_token": new_session.phantom_token,
                "hmac_key": new_session.hmac_key,
                "expires_at": new_session.expires_at,
            }
        )

    @app.delete("/sessions/{session_token}")
    async def revoke_session(session_token: str) -> JSONResponse:
        """Immediately revoke a session. Works in both Redis and token-encoded modes."""
        revoked = await state.session_manager.revoke_session(session_token)
        state.token_store.revoke(session_token)

        if revoked:
            return JSONResponse({"status": "revoked"})
        return JSONResponse({"error": "Session not found"}, status_code=404)

    # --- Capabilities endpoint ---

    @app.get("/capabilities")
    async def get_capabilities(request: Request) -> JSONResponse:
        """Return the agent's effective capabilities based on its session and policy.

        Requires authentication (phantom token or Bearer).
        Returns: agent_id, mode, roles, allowed_tools, rate_limit.
        """
        session = request.scope.get("state", {}).get("phantom_session")
        if session is None:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)

        agent_id = session.agent_id
        access = settings.access.get(agent_id) or settings.access.get("default")
        if access is None:
            return JSONResponse(
                {"error": f"No access policy for agent: {agent_id}"},
                status_code=403,
            )

        return JSONResponse({
            "agent_id": agent_id,
            "mode": access.mode,
            "roles": access.roles,
            "allowed_tools": access.tools,
            "rate_limit": {
                "requests_per_minute": access.rate_limit.requests_per_minute,
            },
        })

    # --- Dynamic tool list updates ---

    @app.post("/notifications/tools/list_changed")
    async def tools_list_changed(request: Request) -> JSONResponse:
        """Handle notifications/tools/list_changed from upstream MCP servers.

        Re-filters tool lists when permissions change mid-session.
        Cerbos supports live policy reloading without restarts.
        """
        logger.info("tools_list_changed_notification_received")
        return JSONResponse({"status": "acknowledged"})

    return app


async def _periodic_eviction(state: GatewayState) -> None:
    """Background task: evict expired pooled sessions and update key age metric."""
    while True:
        try:
            await asyncio.sleep(60)
            await state.session_pool.evict_expired()
            state.token_store.cleanup_expired()
            state.rotation_manager.update_key_age_metric()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("periodic_eviction_error")
