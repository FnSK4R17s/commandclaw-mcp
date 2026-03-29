"""DNS rebinding protection — resolve once, check CIDR deny list, pre-resolved connect."""

import ipaddress
import socket
from ipaddress import IPv4Network, IPv6Network

import structlog

logger = structlog.get_logger()

# RFC 1918 + loopback + link-local + RFC 6598
DEFAULT_DENY_CIDRS: list[IPv4Network | IPv6Network] = [
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
    IPv4Network("127.0.0.0/8"),
    IPv4Network("169.254.0.0/16"),
    IPv4Network("100.64.0.0/10"),
    IPv6Network("::1/128"),
    IPv6Network("fe80::/10"),
    IPv6Network("fc00::/7"),
]


class DNSRebindingError(Exception):
    pass


def resolve_and_check(
    hostname: str,
    deny_cidrs: list[IPv4Network | IPv6Network] | None = None,
) -> str:
    """Resolve hostname to IP, check against deny list, return resolved IP.

    Raises DNSRebindingError if any resolved IP falls within a denied CIDR.
    """
    cidrs = deny_cidrs if deny_cidrs is not None else DEFAULT_DENY_CIDRS

    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DNSRebindingError(f"DNS resolution failed for {hostname}") from exc

    if not results:
        raise DNSRebindingError(f"No DNS results for {hostname}")

    # Use the first result
    ip_str = results[0][4][0]
    ip_addr = ipaddress.ip_address(ip_str)

    for cidr in cidrs:
        if ip_addr in cidr:
            logger.warning(
                "dns_rebinding_blocked",
                hostname=hostname,
                resolved_ip=ip_str,
                blocked_cidr=str(cidr),
            )
            raise DNSRebindingError(
                f"Resolved IP {ip_str} for {hostname} is in denied CIDR {cidr}"
            )

    logger.debug("dns_resolved", hostname=hostname, resolved_ip=ip_str)
    return ip_str
