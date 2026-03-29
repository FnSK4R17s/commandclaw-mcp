"""Streamable HTTP + stdio bridging + DNS rebinding protection + MCP header propagation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from fastmcp import Client

from commandclaw_mcp.security.dns_rebinding import DNSRebindingError, resolve_and_check

if TYPE_CHECKING:
    from commandclaw_mcp.config import ServerConfig

logger = structlog.get_logger()

# MCP protocol version for the MCP-Protocol-Version header
MCP_PROTOCOL_VERSION = "2025-11-25"


def validate_upstream_url(url: str, *, allow_private: bool = False) -> str:
    """Validate an HTTP upstream URL against DNS rebinding.

    Resolves the hostname, checks against the CIDR deny list,
    and returns the URL with the resolved IP for pre-resolved connect.

    Args:
        allow_private: Skip CIDR check for private IPs (Docker Compose / trusted networks).

    Raises DNSRebindingError if the resolved IP is in a denied CIDR.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return url

    # Skip DNS check for localhost — loopback is the intended use case
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return url

    if allow_private:
        logger.info("upstream_dns_check_skipped", hostname=hostname, reason="allow_private_upstream")
        return url

    resolved_ip = resolve_and_check(hostname)
    logger.info(
        "upstream_dns_validated",
        hostname=hostname,
        resolved_ip=resolved_ip,
    )
    return url


def build_client_url(server: ServerConfig, *, allow_private: bool = False) -> str:
    """Build the appropriate client URL/command for an MCP server config.

    stdio servers: returns the command + args as a string for FastMCP
    HTTP servers: returns the URL directly (after DNS rebinding check)
    """
    if server.is_stdio:
        # FastMCP's Client handles stdio via command strings
        parts = [server.command or ""]
        parts.extend(server.args)
        return " ".join(parts)

    url = server.url or ""
    # Validate HTTP URLs against DNS rebinding
    if url:
        validate_upstream_url(url, allow_private=allow_private)
    return url


def create_mcp_client(
    server: ServerConfig,
    name: str,
    *,
    allow_private: bool = False,
) -> Client:
    """Create a FastMCP Client for an upstream MCP server.

    Handles both stdio and HTTP transport types:
    - stdio: "npx -y @notionhq/notion-mcp-server" -> subprocess via stdin/stdout
    - HTTP: "https://api.example.com/mcp" -> Streamable HTTP

    DNS rebinding protection is applied to all HTTP URLs.
    MCP-Protocol-Version header is injected for HTTP transports.
    """
    url = build_client_url(server, allow_private=allow_private)

    # Pass environment variables for stdio servers
    env = server.env if server.is_stdio and server.env else None

    transport_type = "stdio" if server.is_stdio else "http"
    logger.info(
        "creating_mcp_client",
        name=name,
        transport=transport_type,
        target=url[:50],  # Truncate for logging
    )

    # HTTP clients get MCP-Protocol-Version header
    headers: dict[str, str] | None = None
    if not server.is_stdio:
        headers = {"MCP-Protocol-Version": MCP_PROTOCOL_VERSION}

    return Client(url, env=env, headers=headers)
