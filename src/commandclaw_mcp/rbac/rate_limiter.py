"""Per-agent, per-tool rate limiting backed by Redis."""

from __future__ import annotations

import structlog
from redis.asyncio import Redis

from commandclaw_mcp.observability.metrics import rate_limit_rejections_total

logger = structlog.get_logger()

_RATE_LIMIT_PREFIX = "mcp:ratelimit:"


class RateLimitExceeded(Exception):
    """Raised when an agent exceeds its rate limit."""

    def __init__(self, agent_id: str, tool: str, limit: int) -> None:
        self.agent_id = agent_id
        self.tool = tool
        self.limit = limit
        super().__init__(
            f"Agent '{agent_id}' exceeded rate limit for tool '{tool}': {limit}/min"
        )


class RateLimiter:
    """Sliding-window rate limiter using Redis INCR + EXPIRE.

    Per-agent and per-tool limits. RBAC controls what an agent can do;
    rate limiting controls how fast. Both are required.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(
        self,
        *,
        agent_id: str,
        tool: str,
        requests_per_minute: int,
    ) -> None:
        """Check rate limit. Raises RateLimitExceeded if over limit.

        Uses Redis INCR with 60-second TTL for a simple sliding window.
        """
        key = f"{_RATE_LIMIT_PREFIX}{agent_id}:{tool}"
        window = 60  # 1 minute window

        current = await self._redis.incr(key)
        if current == 1:
            # First request in this window — set expiry
            await self._redis.expire(key, window)

        if current > requests_per_minute:
            rate_limit_rejections_total.labels(agent_id=agent_id, tool=tool).inc()
            logger.warning(
                "rate_limit_exceeded",
                agent_id=agent_id,
                tool=tool,
                current=current,
                limit=requests_per_minute,
            )
            raise RateLimitExceeded(agent_id, tool, requests_per_minute)

    async def get_remaining(
        self,
        *,
        agent_id: str,
        tool: str,
        requests_per_minute: int,
    ) -> int:
        """Get remaining requests in the current window."""
        key = f"{_RATE_LIMIT_PREFIX}{agent_id}:{tool}"
        current = await self._redis.get(key)
        if current is None:
            return requests_per_minute
        return max(0, requests_per_minute - int(current))
