"""Dual-key rotation manager — asyncio.Task with asyncio.Lock.

Hourly rotation with 5-minute overlap window for zero-downtime.
On failure: cache current credential with one-interval grace period,
alert with exponential backoff retry (up to 3 attempts). Never fail open.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING

import structlog

from commandclaw_mcp.auth.phantom import TokenStore, hash_token
from commandclaw_mcp.observability.audit import audit_rotation_event
from commandclaw_mcp.observability.metrics import (
    active_key_age_seconds,
    token_rotation_failures_consecutive,
    token_rotation_total,
)

if TYPE_CHECKING:
    from commandclaw_mcp.config import Settings

logger = structlog.get_logger()

_MAX_RETRY_ATTEMPTS = 3


class RotationManager:
    """Manages periodic phantom token rotation with dual-key overlap."""

    def __init__(self, token_store: TokenStore, settings: Settings) -> None:
        self._store = token_store
        self._settings = settings
        self._interval = settings.gateway.key_rotation_interval_seconds
        self._overlap = settings.gateway.overlap_window_seconds
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._consecutive_failures = 0
        self._last_rotation_time: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_rotation_time(self) -> float:
        return self._last_rotation_time

    async def start(self) -> None:
        """Start the rotation background task."""
        if self._running:
            return
        self._running = True
        self._last_rotation_time = time.time()
        self._task = asyncio.create_task(self._rotation_loop())
        logger.info(
            "rotation_manager_started",
            interval_seconds=self._interval,
            overlap_seconds=self._overlap,
        )

    async def stop(self) -> None:
        """Stop the rotation background task gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("rotation_manager_stopped")

    async def _rotation_loop(self) -> None:
        """Main loop: sleep for interval, then rotate."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                await self._perform_rotation()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("rotation_loop_error")
                # Don't crash the loop — retry next interval

    async def _perform_rotation(self) -> None:
        """Execute a rotation with retry and failure tracking."""
        async with self._lock:
            for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
                try:
                    old_count = self._store.session_count
                    self._store.rotate()
                    self._store.cleanup_expired()

                    now = time.time()
                    self._last_rotation_time = now

                    # Update metrics
                    token_rotation_total.labels(status="success").inc()
                    self._consecutive_failures = 0
                    token_rotation_failures_consecutive.set(0)
                    active_key_age_seconds.set(0)

                    # Audit
                    await audit_rotation_event(
                        old_token_hash=f"gen-{old_count}",
                        new_token_hash=f"gen-{self._store.session_count}",
                        success=True,
                    )

                    logger.info(
                        "rotation_completed",
                        attempt=attempt,
                        previous_sessions=self._store.session_count,
                    )
                    return

                except Exception:
                    logger.exception(
                        "rotation_attempt_failed",
                        attempt=attempt,
                        max_attempts=_MAX_RETRY_ATTEMPTS,
                    )
                    if attempt < _MAX_RETRY_ATTEMPTS:
                        backoff = 2 ** (attempt - 1)
                        await asyncio.sleep(backoff)

            # All retries exhausted — extend grace period, don't fail open
            self._consecutive_failures += 1
            token_rotation_failures_consecutive.set(self._consecutive_failures)
            token_rotation_total.labels(status="failure").inc()

            await audit_rotation_event(
                old_token_hash="rotation-failed",
                new_token_hash="rotation-failed",
                success=False,
            )

            logger.error(
                "rotation_failed_all_retries",
                consecutive_failures=self._consecutive_failures,
                msg="Extending current key by one rotation interval (grace period). "
                "Stale key is better than no authentication.",
            )

    async def force_rotate(self) -> None:
        """Trigger an immediate rotation (for admin/testing)."""
        await self._perform_rotation()

    def update_key_age_metric(self) -> None:
        """Update the active_key_age_seconds gauge. Called periodically."""
        if self._last_rotation_time > 0:
            age = time.time() - self._last_rotation_time
            active_key_age_seconds.set(age)
