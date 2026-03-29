"""RBAC middleware — wires discovery_filter + call_guard + rate_limiter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from commandclaw_mcp.auth.credential_store import PhantomSession
    from commandclaw_mcp.config import Settings
    from commandclaw_mcp.rbac.call_guard import CallGuard
    from commandclaw_mcp.rbac.discovery_filter import DiscoveryFilter
    from commandclaw_mcp.rbac.rate_limiter import RateLimiter

logger = structlog.get_logger()


class RBACHandler:
    """Unified RBAC handler wiring discovery filtering, call-time enforcement, and rate limiting.

    Dual-layer enforcement:
    - Discovery: filter tools/list so agents can't see unauthorized tools
    - Call-time: enforce ABAC policies on tools/call with full invocation context
    - Rate limiting: control how fast agents can call tools
    """

    def __init__(
        self,
        discovery_filter: DiscoveryFilter,
        call_guard: CallGuard,
        rate_limiter: RateLimiter,
        settings: Settings,
    ) -> None:
        self._discovery = discovery_filter
        self._guard = call_guard
        self._rate_limiter = rate_limiter
        self._settings = settings

    def _get_roles(self, agent_id: str) -> list[str]:
        """Get roles for an agent from config."""
        access = self._settings.access.get(agent_id)
        if access is None:
            return []
        return access.roles

    def _get_rate_limit(self, agent_id: str) -> int:
        """Get rate limit (requests per minute) for an agent."""
        access = self._settings.access.get(agent_id)
        if access is None:
            return 0  # Unknown agent = no access
        return access.rate_limit.requests_per_minute

    async def filter_tool_list(
        self,
        agent_id: str,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Filter tools/list response based on agent permissions."""
        roles = self._get_roles(agent_id)
        if not roles:
            logger.warning("rbac_no_roles", agent_id=agent_id)
            return []

        return await self._discovery.filter_tools(
            agent_id=agent_id,
            roles=roles,
            tools=tools,
        )

    async def authorize_call(
        self,
        agent_id: str,
        tool_name: str,
        resource_attrs: dict[str, Any] | None = None,
    ) -> None:
        """Authorize a tools/call request. Raises on denial or rate limit.

        Checks both RBAC policy (is the agent allowed?) and rate limit
        (is the agent going too fast?).
        """
        roles = self._get_roles(agent_id)
        if not roles:
            from commandclaw_mcp.rbac.call_guard import CallGuardDenied

            raise CallGuardDenied(agent_id, tool_name, reason="no roles configured")

        # Call-time RBAC check
        await self._guard.check(
            agent_id=agent_id,
            roles=roles,
            tool_name=tool_name,
            resource_attrs=resource_attrs,
        )

        # Rate limit check
        rpm = self._get_rate_limit(agent_id)
        if rpm > 0:
            await self._rate_limiter.check(
                agent_id=agent_id,
                tool=tool_name,
                requests_per_minute=rpm,
            )
