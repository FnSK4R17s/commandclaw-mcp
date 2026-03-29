"""MCP JSON-RPC 2.0 error codes and error response builders.

Standard JSON-RPC 2.0 error codes:
  -32700  Parse error
  -32600  Invalid request
  -32601  Method not found
  -32602  Invalid params
  -32603  Internal error
  -32000 to -32099  Server error (reserved for implementation)

MCP-specific behavior:
  - Gateway maps RBAC denials to -32600 (invalid request — tool not available)
  - Rate limit exceeded maps to -32000 (server error — retry after)
  - HMAC failures are HTTP 401 (handled by auth_middleware, not JSON-RPC)
  - Upstream errors are proxied as-is with original error codes preserved
"""

from __future__ import annotations

from typing import Any


# Standard JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP server error range (-32000 to -32099)
RATE_LIMITED = -32000
RBAC_DENIED = -32001
UPSTREAM_UNAVAILABLE = -32002
CREDENTIAL_ERROR = -32003
CIRCUIT_OPEN = -32004


def jsonrpc_error(
    code: int,
    message: str,
    request_id: str | int | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response.

    The `id` field MUST match the original request's id.
    For notifications (no id), no response is generated.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }


def rbac_denied_error(
    request_id: str | int | None,
    tool_name: str,
    agent_id: str,
) -> dict[str, Any]:
    """RBAC denial: agent not authorized for this tool."""
    return jsonrpc_error(
        code=RBAC_DENIED,
        message=f"Access denied: agent '{agent_id}' is not authorized for tool '{tool_name}'",
        request_id=request_id,
        data={"tool": tool_name, "agent_id": agent_id},
    )


def rate_limited_error(
    request_id: str | int | None,
    tool_name: str,
    agent_id: str,
    limit: int,
) -> dict[str, Any]:
    """Rate limit exceeded for this agent + tool combination."""
    return jsonrpc_error(
        code=RATE_LIMITED,
        message=f"Rate limit exceeded: {limit} requests/min for tool '{tool_name}'",
        request_id=request_id,
        data={"tool": tool_name, "agent_id": agent_id, "limit_rpm": limit},
    )


def upstream_unavailable_error(
    request_id: str | int | None,
    server_name: str,
) -> dict[str, Any]:
    """Upstream MCP server is unavailable (circuit breaker open)."""
    return jsonrpc_error(
        code=UPSTREAM_UNAVAILABLE,
        message=f"Upstream server '{server_name}' is temporarily unavailable",
        request_id=request_id,
        data={"server": server_name},
    )


def credential_error(
    request_id: str | int | None,
    server_name: str,
) -> dict[str, Any]:
    """Credential injection failed (expired, missing, decrypt error)."""
    return jsonrpc_error(
        code=CREDENTIAL_ERROR,
        message=f"Credential error for server '{server_name}'",
        request_id=request_id,
        data={"server": server_name},
    )


def circuit_open_error(
    request_id: str | int | None,
    server_name: str,
) -> dict[str, Any]:
    """Circuit breaker is open for upstream server."""
    return jsonrpc_error(
        code=CIRCUIT_OPEN,
        message=f"Circuit breaker open for '{server_name}' — too many consecutive failures",
        request_id=request_id,
        data={"server": server_name},
    )


def parse_error(request_id: str | int | None = None) -> dict[str, Any]:
    """JSON parse error."""
    return jsonrpc_error(
        code=PARSE_ERROR,
        message="Parse error: invalid JSON",
        request_id=request_id,
    )


def method_not_found_error(
    request_id: str | int | None,
    method: str,
) -> dict[str, Any]:
    """Method not found."""
    return jsonrpc_error(
        code=METHOD_NOT_FOUND,
        message=f"Method not found: '{method}'",
        request_id=request_id,
    )
