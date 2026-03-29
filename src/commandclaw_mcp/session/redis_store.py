"""Redis-backed session store with TTL and connection pooling."""

from __future__ import annotations

import json
from typing import Any

import structlog
from redis.asyncio import ConnectionPool, Redis

from commandclaw_mcp.observability.metrics import active_sessions

logger = structlog.get_logger()

_SESSION_PREFIX = "mcp:session:"


class RedisSessionStore:
    """Session store backed by Redis with automatic TTL expiry."""

    def __init__(self, redis_url: str, default_ttl: int = 3600) -> None:
        self._pool = ConnectionPool.from_url(redis_url, decode_responses=True)
        self._redis = Redis(connection_pool=self._pool)
        self._default_ttl = default_ttl

    async def close(self) -> None:
        """Close the Redis connection pool."""
        await self._redis.aclose()
        await self._pool.disconnect()

    async def store(
        self, session_id: str, data: dict[str, Any], ttl: int | None = None
    ) -> None:
        """Store session data with TTL."""
        key = f"{_SESSION_PREFIX}{session_id}"
        await self._redis.setex(key, ttl or self._default_ttl, json.dumps(data))
        active_sessions.inc()
        logger.debug("session_stored", session_id=session_id[:8])

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """Retrieve session data. Returns None if expired or not found."""
        key = f"{_SESSION_PREFIX}{session_id}"
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def delete(self, session_id: str) -> bool:
        """Immediately revoke a session. Returns True if it existed."""
        key = f"{_SESSION_PREFIX}{session_id}"
        result = await self._redis.delete(key)
        if result:
            active_sessions.dec()
            logger.info("session_deleted", session_id=session_id[:8])
        return bool(result)

    async def exists(self, session_id: str) -> bool:
        """Check if a session exists."""
        key = f"{_SESSION_PREFIX}{session_id}"
        return bool(await self._redis.exists(key))

    async def refresh_ttl(self, session_id: str, ttl: int | None = None) -> bool:
        """Refresh a session's TTL. Returns False if not found."""
        key = f"{_SESSION_PREFIX}{session_id}"
        return bool(await self._redis.expire(key, ttl or self._default_ttl))

    async def health_check(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(await self._redis.ping())
        except Exception:
            logger.exception("redis_health_check_failed")
            return False
