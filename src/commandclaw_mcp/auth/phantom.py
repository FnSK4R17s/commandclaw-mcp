"""Phantom token generation, PhantomSession management, and TokenStore."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import TYPE_CHECKING

import structlog

from commandclaw_mcp.auth.credential_store import CredentialEntry, PhantomSession
from commandclaw_mcp.observability.metrics import active_sessions

if TYPE_CHECKING:
    from commandclaw_mcp.config import Settings

logger = structlog.get_logger()


def generate_phantom_token() -> str:
    """Generate a 256-bit random opaque token."""
    return secrets.token_urlsafe(32)


def generate_hmac_key() -> str:
    """Generate a 256-bit random HMAC signing key."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hash of a token for audit logging (never log raw tokens)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class TokenStore:
    """In-memory store for phantom sessions with dual-generation support.

    Supports two key generations for zero-downtime rotation:
    - current: the active generation
    - previous: still valid during the overlap window
    """

    def __init__(self) -> None:
        self._current: dict[str, PhantomSession] = {}
        self._previous: dict[str, PhantomSession] = {}

    def create_session(
        self,
        agent_id: str,
        credentials: dict[str, CredentialEntry],
        ttl_seconds: int = 3600,
    ) -> PhantomSession:
        """Create a new phantom session for an agent."""
        now = time.time()
        session = PhantomSession(
            phantom_token=generate_phantom_token(),
            hmac_key=generate_hmac_key(),
            agent_id=agent_id,
            credentials=credentials,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        self._current[session.phantom_token] = session
        active_sessions.inc()
        logger.info(
            "session_created",
            agent_id=agent_id,
            token_hash=hash_token(session.phantom_token),
        )
        return session

    def lookup(self, token: str) -> PhantomSession | None:
        """Look up a session by phantom token. Checks current gen, falls back to previous."""
        now = time.time()

        # Check current generation first
        session = self._current.get(token)
        if session and session.expires_at > now:
            return session

        # Fall back to previous generation (overlap window)
        session = self._previous.get(token)
        if session and session.expires_at > now:
            return session

        return None

    def revoke(self, token: str) -> bool:
        """Immediately revoke a phantom token. Returns True if found."""
        removed = False
        if token in self._current:
            del self._current[token]
            removed = True
        if token in self._previous:
            del self._previous[token]
            removed = True
        if removed:
            active_sessions.dec()
            logger.info("session_revoked", token_hash=hash_token(token))
        return removed

    def rotate(self) -> None:
        """Promote current generation to previous, start fresh current.

        Called by the rotation manager. Old previous tokens are discarded —
        they've had the full overlap window to complete in-flight requests.
        """
        expired_count = len(self._previous)
        self._previous = self._current
        self._current = {}
        active_sessions.set(len(self._previous))  # Only previous gen remains
        logger.info(
            "token_generation_rotated",
            previous_sessions=len(self._previous),
            expired_sessions=expired_count,
        )

    def cleanup_expired(self) -> int:
        """Remove expired sessions from both generations. Returns count removed."""
        now = time.time()
        removed = 0

        for store in (self._current, self._previous):
            expired_tokens = [t for t, s in store.items() if s.expires_at <= now]
            for token in expired_tokens:
                del store[token]
                removed += 1

        if removed:
            active_sessions.dec(removed)
            logger.info("expired_sessions_cleaned", count=removed)
        return removed

    @property
    def session_count(self) -> int:
        return len(self._current) + len(self._previous)

    def create_session_from_config(
        self,
        agent_id: str,
        settings: Settings,
    ) -> PhantomSession | None:
        """Create a session using the agent's access config and server configs."""
        access = settings.access.get(agent_id)
        if access is None:
            logger.warning("unknown_agent", agent_id=agent_id)
            return None

        credentials: dict[str, CredentialEntry] = {}
        for tool_name in access.tools:
            server = settings.servers.get(tool_name)
            if server is None:
                continue
            url = server.url or f"stdio://{tool_name}"
            credentials[tool_name] = CredentialEntry(
                real_credential="",  # Credentials loaded from encrypted store at injection time
                upstream_url=url,
            )

        return self.create_session(
            agent_id=agent_id,
            credentials=credentials,
            ttl_seconds=settings.gateway.key_rotation_interval_seconds,
        )
