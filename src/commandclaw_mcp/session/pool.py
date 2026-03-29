"""Upstream MCP session pooling with circuit breaker."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

import structlog

from commandclaw_mcp.observability.metrics import circuit_breaker_state, session_pool_active

logger = structlog.get_logger()


class CircuitState(Enum):
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


@dataclass
class PooledSession:
    """A pooled upstream MCP session."""

    session_id: str
    server_url: str
    identity_hash: str
    transport_type: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    # MCP protocol: Mcp-Session-Id assigned by upstream server
    mcp_session_id: str | None = None
    # SSE resumability: last event ID for reconnection via Last-Event-ID header
    last_event_id: str | None = None

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def idle_time(self) -> float:
        return time.time() - self.last_used


@dataclass
class CircuitBreaker:
    """Per-upstream circuit breaker."""

    upstream: str
    threshold: int = 5
    reset_seconds: int = 60
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0

    def record_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            logger.info("circuit_breaker_closed", upstream=self.upstream)
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        circuit_breaker_state.labels(upstream=self.upstream).set(CircuitState.CLOSED.value)

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.threshold:
            self.state = CircuitState.OPEN
            circuit_breaker_state.labels(upstream=self.upstream).set(CircuitState.OPEN.value)
            logger.warning(
                "circuit_breaker_opened",
                upstream=self.upstream,
                failures=self.failure_count,
            )

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.reset_seconds:
                self.state = CircuitState.HALF_OPEN
                circuit_breaker_state.labels(upstream=self.upstream).set(
                    CircuitState.HALF_OPEN.value
                )
                logger.info("circuit_breaker_half_open", upstream=self.upstream)
                return True  # Allow one trial request
            return False
        # HALF_OPEN — allow one trial request (already allowed once, block further)
        return False


PoolKey = tuple[str, str, str]  # (server_url, identity_hash, transport_type)


class SessionPool:
    """Upstream MCP session pool with per-key limits and circuit breakers."""

    def __init__(
        self,
        max_per_key: int = 10,
        ttl_seconds: int = 300,
        idle_eviction_seconds: int = 600,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_reset_seconds: int = 60,
    ) -> None:
        self._max_per_key = max_per_key
        self._ttl = ttl_seconds
        self._idle_eviction = idle_eviction_seconds
        self._cb_threshold = circuit_breaker_threshold
        self._cb_reset = circuit_breaker_reset_seconds
        self._pools: dict[PoolKey, list[PooledSession]] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    def _get_breaker(self, upstream: str) -> CircuitBreaker:
        if upstream not in self._breakers:
            self._breakers[upstream] = CircuitBreaker(
                upstream=upstream,
                threshold=self._cb_threshold,
                reset_seconds=self._cb_reset,
            )
        return self._breakers[upstream]

    async def acquire(self, key: PoolKey) -> PooledSession | None:
        """Get a pooled session for the given key, or None if none available."""
        server_url = key[0]
        breaker = self._get_breaker(server_url)

        if not breaker.allow_request():
            logger.debug("circuit_breaker_rejected", upstream=server_url)
            return None

        async with self._lock:
            sessions = self._pools.get(key, [])
            now = time.time()

            # Find a valid session
            for i, session in enumerate(sessions):
                if session.age <= self._ttl and session.idle_time <= self._idle_eviction:
                    session.last_used = now
                    sessions.pop(i)  # Remove from pool (caller holds it)
                    session_pool_active.labels(upstream=server_url).dec()
                    return session

            return None

    async def release(self, key: PoolKey, session: PooledSession) -> None:
        """Return a session to the pool after use."""
        server_url = key[0]
        breaker = self._get_breaker(server_url)
        breaker.record_success()

        async with self._lock:
            if key not in self._pools:
                self._pools[key] = []

            pool = self._pools[key]
            if len(pool) < self._max_per_key and session.age <= self._ttl:
                session.last_used = time.time()
                pool.append(session)
                session_pool_active.labels(upstream=server_url).inc()
            else:
                logger.debug("session_not_returned_to_pool", upstream=server_url)

    async def report_failure(self, server_url: str) -> None:
        """Report a connection failure to the circuit breaker."""
        breaker = self._get_breaker(server_url)
        breaker.record_failure()

    async def evict_expired(self) -> int:
        """Remove expired and idle sessions. Returns count evicted."""
        evicted = 0
        async with self._lock:
            for key, sessions in list(self._pools.items()):
                server_url = key[0]
                before = len(sessions)
                sessions[:] = [
                    s for s in sessions
                    if s.age <= self._ttl and s.idle_time <= self._idle_eviction
                ]
                diff = before - len(sessions)
                if diff > 0:
                    evicted += diff
                    session_pool_active.labels(upstream=server_url).dec(diff)

                if not sessions:
                    del self._pools[key]

        if evicted:
            logger.info("pool_sessions_evicted", count=evicted)
        return evicted

    @property
    def total_pooled(self) -> int:
        return sum(len(s) for s in self._pools.values())
