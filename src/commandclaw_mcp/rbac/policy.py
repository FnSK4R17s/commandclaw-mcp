"""Cerbos async HTTP client for RBAC policy decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from cerbos.sdk.client import AsyncCerbosClient
from cerbos.sdk.model import Principal, Resource, ResourceList

if TYPE_CHECKING:
    from commandclaw_mcp.config import CerbosConfig

logger = structlog.get_logger()


class PolicyEngine:
    """Async Cerbos client for tool RBAC decisions. Deny-by-default."""

    def __init__(self, config: CerbosConfig) -> None:
        self._config = config
        self._client: AsyncCerbosClient | None = None

    async def connect(self) -> None:
        """Establish connection to Cerbos PDP."""
        scheme = "https" if self._config.tls else "http"
        host = f"{scheme}://{self._config.host}:{self._config.port}"
        client = AsyncCerbosClient(host)
        self._client = await client.__aenter__()
        logger.info("cerbos_connected", host=host)

    async def close(self) -> None:
        """Close the Cerbos connection."""
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def check_tool_access(
        self,
        *,
        agent_id: str,
        roles: list[str],
        tool_name: str,
        resource_attrs: dict[str, Any] | None = None,
    ) -> bool:
        """Check if an agent with given roles can access a specific tool.

        Returns False if Cerbos is unreachable (deny-by-default).
        """
        if not self._client:
            logger.error("cerbos_not_connected", agent_id=agent_id, tool=tool_name)
            return False

        principal = Principal(id=agent_id, roles=roles)
        resources = ResourceList()
        resources.add(
            Resource(id=tool_name, kind="mcp::tools", attr=resource_attrs or {}),
            actions={tool_name},
        )

        try:
            resp = await self._client.check_resources(
                principal=principal,
                resources=resources,
            )
            result = resp.get_resource(tool_name)
            return result is not None and result.is_allowed(tool_name)
        except Exception:
            logger.exception("cerbos_check_failed", agent_id=agent_id, tool=tool_name)
            return False  # Deny-by-default

    async def filter_tools(
        self,
        *,
        agent_id: str,
        roles: list[str],
        tool_names: list[str],
    ) -> list[str]:
        """Batch filter: return only tools the agent is allowed to see.

        Uses Cerbos batch checkResource — one network call filters all tools.
        Returns empty list if Cerbos is unreachable (deny-by-default).
        """
        if not self._client:
            logger.error("cerbos_not_connected_for_filter", agent_id=agent_id)
            return []

        if not tool_names:
            return []

        principal = Principal(id=agent_id, roles=roles)
        resources = ResourceList()
        for name in tool_names:
            resources.add(Resource(id=name, kind="mcp::tools"), actions={name})

        try:
            resp = await self._client.check_resources(
                principal=principal,
                resources=resources,
            )
            allowed = []
            for name in tool_names:
                result = resp.get_resource(name)
                if result is not None and result.is_allowed(name):
                    allowed.append(name)
            return allowed
        except Exception:
            logger.exception("cerbos_filter_failed", agent_id=agent_id)
            return []  # Deny-by-default

    async def health_check(self) -> bool:
        """Check if Cerbos PDP is reachable.

        A denied result still means Cerbos is healthy — deny-by-default is expected.
        """
        if not self._client:
            return False
        try:
            principal = Principal(id="health-check", roles=["_health"])
            resources = ResourceList()
            resources.add(Resource(id="_ping", kind="mcp::tools"), actions={"_ping"})
            await self._client.check_resources(principal=principal, resources=resources)
            return True  # Connection works — allow/deny result doesn't matter
        except Exception:
            return False
