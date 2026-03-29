"""Call-time RBAC enforcement — ABAC checks before forwarding tool calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from commandclaw_mcp.observability.metrics import rbac_decisions_total

if TYPE_CHECKING:
    from commandclaw_mcp.rbac.policy import PolicyEngine

logger = structlog.get_logger()


class CallGuardDenied(Exception):
    """Raised when a tool call is denied by RBAC policy."""

    def __init__(self, agent_id: str, tool: str, reason: str = "denied by policy") -> None:
        self.agent_id = agent_id
        self.tool = tool
        self.reason = reason
        super().__init__(f"Agent '{agent_id}' denied access to tool '{tool}': {reason}")


class CallGuard:
    """Enforces RBAC at call time with full invocation context.

    Discovery filtering alone can be bypassed by raw HTTP requests.
    Call-time enforcement catches runtime conditions (amount, department,
    time-of-day) that discovery-time permissions can't express.
    """

    def __init__(self, policy_engine: PolicyEngine) -> None:
        self._policy = policy_engine

    async def check(
        self,
        *,
        agent_id: str,
        roles: list[str],
        tool_name: str,
        resource_attrs: dict[str, Any] | None = None,
    ) -> None:
        """Check if a tool call is allowed. Raises CallGuardDenied if not.

        resource_attrs can include: amount, department, time-of-day,
        or any ABAC context the Cerbos policy needs.
        """
        allowed = await self._policy.check_tool_access(
            agent_id=agent_id,
            roles=roles,
            tool_name=tool_name,
            resource_attrs=resource_attrs,
        )

        decision = "allow" if allowed else "deny"
        rbac_decisions_total.labels(
            agent_id=agent_id, tool=tool_name, decision=decision
        ).inc()

        if not allowed:
            logger.warning(
                "call_guard_denied",
                agent_id=agent_id,
                tool=tool_name,
                attrs=resource_attrs,
            )
            raise CallGuardDenied(agent_id, tool_name)

        logger.debug("call_guard_allowed", agent_id=agent_id, tool=tool_name)
