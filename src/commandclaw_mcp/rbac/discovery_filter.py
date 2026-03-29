"""Discovery filtering middleware — filter tools/list by agent principal + tool count limit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from commandclaw_mcp.observability.metrics import rbac_decisions_total

if TYPE_CHECKING:
    from commandclaw_mcp.rbac.policy import PolicyEngine

logger = structlog.get_logger()

# Default: 0 = unlimited. Set via config gateway.max_tools_per_session.
DEFAULT_MAX_TOOLS = 0


class DiscoveryFilter:
    """Filters tool lists based on agent RBAC permissions.

    Constrains the LLM's reasoning space — the agent doesn't know
    unauthorized tools exist, so it can't attempt to use them.

    Also enforces per-session tool count limits (Virtual MCP pattern)
    to prevent LLM context overload. When max_tools > 0, only the
    first max_tools allowed tools are returned, reducing token usage
    by 60-85% in large deployments.
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        max_tools_per_session: int = DEFAULT_MAX_TOOLS,
    ) -> None:
        self._policy = policy_engine
        self._max_tools = max_tools_per_session

    async def filter_tools(
        self,
        *,
        agent_id: str,
        roles: list[str],
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Filter a list of tool descriptors. Returns only allowed tools.

        New sessions start with zero tools. Only tools explicitly allowed
        by policy become visible. If Cerbos is unreachable, return empty list.

        When max_tools_per_session > 0, caps the returned list to prevent
        LLM context overload (Virtual MCP pattern).
        """
        if not tools:
            return []

        tool_names = [t.get("name", "") for t in tools if t.get("name")]
        allowed_names = await self._policy.filter_tools(
            agent_id=agent_id,
            roles=roles,
            tool_names=tool_names,
        )

        allowed_set = set(allowed_names)
        filtered = [t for t in tools if t.get("name") in allowed_set]

        # Metrics
        for name in tool_names:
            decision = "allow" if name in allowed_set else "deny"
            rbac_decisions_total.labels(
                agent_id=agent_id, tool=name, decision=decision
            ).inc()

        # Virtual MCP pattern: cap tool count to reduce LLM context
        if self._max_tools > 0 and len(filtered) > self._max_tools:
            logger.info(
                "tool_count_limited",
                agent_id=agent_id,
                total_allowed=len(filtered),
                max_tools=self._max_tools,
            )
            filtered = filtered[: self._max_tools]

        logger.info(
            "discovery_filtered",
            agent_id=agent_id,
            total_tools=len(tools),
            allowed_tools=len(filtered),
        )
        return filtered
