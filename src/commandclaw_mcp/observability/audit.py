"""Structured audit logging — no sensitive data, credential-stripping processor."""

from __future__ import annotations

import re
import time
from typing import Any

import structlog

_SENSITIVE_PATTERNS = re.compile(
    r".*(token|key|secret|credential|password).*", re.IGNORECASE
)


def strip_sensitive_fields(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: strip fields matching sensitive patterns before logging."""
    keys_to_strip = [k for k in event_dict if _SENSITIVE_PATTERNS.match(k)]
    for k in keys_to_strip:
        event_dict[k] = "[REDACTED]"
    return event_dict


def configure_logging() -> None:
    """Configure structlog with JSON output and credential stripping."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            strip_sensitive_fields,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


_audit_logger = structlog.get_logger("audit")


async def audit_tool_call(
    *,
    agent_id: str,
    tool: str,
    allowed: bool,
    latency_ms: float,
    server: str | None = None,
) -> None:
    """Log a tool call audit event."""
    _audit_logger.info(
        "tool_call",
        agent_id=agent_id,
        tool=tool,
        allowed=allowed,
        latency_ms=round(latency_ms, 2),
        server=server or "unknown",
        timestamp=time.time(),
    )


async def audit_rotation_event(
    *,
    old_token_hash: str,
    new_token_hash: str,
    success: bool,
) -> None:
    """Log a token rotation audit event."""
    _audit_logger.info(
        "token_rotation",
        old_token_hash=old_token_hash,
        new_token_hash=new_token_hash,
        success=success,
        timestamp=time.time(),
    )


async def audit_session_event(
    *,
    event_type: str,
    session_id_hash: str,
    agent_id: str | None = None,
) -> None:
    """Log a session lifecycle event (create, destroy, expire)."""
    _audit_logger.info(
        "session_event",
        event_type=event_type,
        session_id_hash=session_id_hash,
        agent_id=agent_id or "unknown",
        timestamp=time.time(),
    )
