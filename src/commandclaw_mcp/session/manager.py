"""Unified session manager — switchable between Redis and token-encoded sessions.

Redis (primary): immediate revocation, simpler operations, requires Redis HA for scale.
Token-encoded (scale): any replica can decrypt, no shared state, but needs Redis blocklist for revocation.

The session_mode config controls which backend is active. Both can coexist:
even with token-encoded sessions, Redis serves as nonce cache, rate limiter, and revocation blocklist.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any

import structlog

from commandclaw_mcp.observability.audit import audit_session_event
from commandclaw_mcp.session.redis_store import RedisSessionStore
from commandclaw_mcp.session.token_encoded import (
    Capability,
    FallbackEnabledSessionCrypto,
)

logger = structlog.get_logger()


class SessionMode(str, Enum):
    REDIS = "redis"
    TOKEN_ENCODED = "token_encoded"


class SessionManager:
    """Unified session interface over Redis or token-encoded backends.

    Regardless of mode, Redis is always used for:
    - Nonce cache (HMAC replay prevention)
    - Rate limiting (per-agent, per-tool)
    - Revocation blocklist (token-encoded mode)
    """

    def __init__(
        self,
        mode: SessionMode,
        redis_store: RedisSessionStore,
        token_crypto: FallbackEnabledSessionCrypto | None = None,
    ) -> None:
        self._mode = mode
        self._redis = redis_store
        self._crypto = token_crypto

    @property
    def mode(self) -> SessionMode:
        return self._mode

    async def store_session(
        self,
        session_id: str,
        data: dict[str, Any],
        agent_id: str,
        ttl: int | None = None,
        capabilities: dict[str, int] | None = None,
    ) -> str:
        """Store a session. Returns the session token (Redis ID or encrypted token).

        In Redis mode: stores in Redis, returns session_id as-is.
        In token-encoded mode: encrypts data into the token itself, stores
        a minimal entry in Redis for revocation blocklist checking.
        """
        if self._mode == SessionMode.TOKEN_ENCODED and self._crypto:
            # Encrypt all session data into the token
            encrypted_token = await self._crypto.encrypt(
                data=data,
                subject=agent_id,
                capabilities=capabilities,
            )
            # Store minimal entry in Redis for revocation blocklist
            await self._redis.store(
                _blocklist_key(encrypted_token),
                {"agent_id": agent_id, "active": True},
                ttl=ttl,
            )

            await audit_session_event(
                event_type="create",
                session_id_hash=_hash_token(encrypted_token),
                agent_id=agent_id,
            )
            return encrypted_token
        else:
            # Redis mode: store full data in Redis
            await self._redis.store(session_id, data, ttl=ttl)
            await audit_session_event(
                event_type="create",
                session_id_hash=_hash_token(session_id),
                agent_id=agent_id,
            )
            return session_id

    async def get_session(
        self,
        session_token: str,
        expected_subject: str,
    ) -> dict[str, Any] | None:
        """Retrieve session data.

        In Redis mode: fetches from Redis.
        In token-encoded mode: decrypts from the token itself, then checks
        the Redis blocklist to see if it was revoked.
        """
        if self._mode == SessionMode.TOKEN_ENCODED and self._crypto:
            # Check revocation blocklist first
            blocklist_entry = await self._redis.get(_blocklist_key(session_token))
            if blocklist_entry and not blocklist_entry.get("active", True):
                logger.info("session_revoked_via_blocklist")
                return None

            try:
                return await self._crypto.decrypt(session_token, expected_subject)
            except Exception:
                logger.warning("token_encoded_session_decrypt_failed")
                return None
        else:
            return await self._redis.get(session_token)

    async def revoke_session(
        self,
        session_token: str,
        agent_id: str | None = None,
    ) -> bool:
        """Revoke a session immediately.

        In Redis mode: DELETE the key.
        In token-encoded mode: mark as revoked in the Redis blocklist.
        """
        if self._mode == SessionMode.TOKEN_ENCODED:
            # Mark as revoked in blocklist
            key = _blocklist_key(session_token)
            existing = await self._redis.get(key)
            if existing:
                existing["active"] = False
                await self._redis.store(key, existing)
                await audit_session_event(
                    event_type="destroy",
                    session_id_hash=_hash_token(session_token),
                    agent_id=agent_id,
                )
                return True
            return False
        else:
            result = await self._redis.delete(session_token)
            if result:
                await audit_session_event(
                    event_type="destroy",
                    session_id_hash=_hash_token(session_token),
                    agent_id=agent_id,
                )
            return result

    async def health_check(self) -> bool:
        """Check backend health. Redis is always required."""
        return await self._redis.health_check()


def _hash_token(token: str) -> str:
    """Hash a token for audit logging (never log raw tokens)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _blocklist_key(token: str) -> str:
    """Redis key for the revocation blocklist entry."""
    return f"mcp:blocklist:{_hash_token(token)}"
