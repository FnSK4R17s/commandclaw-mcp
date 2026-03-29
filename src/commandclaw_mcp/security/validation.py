"""Startup config validation — reject defaults, enforce TLS, enforce loopback."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from commandclaw_mcp.config import Settings

logger = structlog.get_logger()

_DEFAULT_SEED = "CHANGE-ME-IN-PRODUCTION"
_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "localhost"})


class ConfigValidationError(Exception):
    pass


def validate_settings(settings: Settings) -> None:
    """Validate settings at startup. Fail fast on dangerous defaults.

    Checks:
    1. Encryption seed must not be the default
    2. Upstream servers must use HTTPS (unless loopback)
    3. Cerbos must use TLS in production (error, not warning)
    4. Non-loopback binding requires explicit allow_non_loopback=true
    5. Session mode must be valid
    """
    errors: list[str] = []

    # 1. Encryption seed
    if settings.gateway.encryption_seed == _DEFAULT_SEED:
        errors.append(
            "encryption_seed is the default value. "
            "Set a unique, random seed in ~/.commandclaw/mcp.json"
        )

    # 2. TLS upstream enforcement
    if settings.gateway.require_tls_upstream:
        for name, server in settings.servers.items():
            if server.url and not server.url.startswith("https://"):
                if not server.url.startswith("http://127.0.0.1") and not server.url.startswith(
                    "http://localhost"
                ):
                    errors.append(
                        f"Server '{name}' URL is not HTTPS and require_tls_upstream is true: "
                        f"{server.url}"
                    )

    # 3. Cerbos TLS enforcement (error in production, not just warning)
    if settings.cerbos.tls is False:
        if settings.cerbos.host not in _LOOPBACK_ADDRESSES:
            if settings.cerbos.allow_plaintext:
                logger.warning(
                    "cerbos_plaintext_allowed",
                    host=settings.cerbos.host,
                    msg="Plaintext gRPC to non-loopback Cerbos — "
                    "acceptable for Docker Compose / trusted networks only",
                )
            else:
                errors.append(
                    "Cerbos gRPC connection to non-loopback host must use TLS. "
                    f"Set cerbos.tls=true, use a loopback address, or set "
                    f"cerbos.allow_plaintext=true for trusted networks "
                    f"(current: {settings.cerbos.host})"
                )
        else:
            logger.warning(
                "cerbos_no_tls",
                msg="Cerbos gRPC on loopback without TLS — acceptable for development only",
            )

    # 4. Non-loopback binding protection
    if settings.gateway.host not in _LOOPBACK_ADDRESSES:
        if not settings.gateway.allow_non_loopback:
            errors.append(
                f"Binding to non-loopback address '{settings.gateway.host}' requires "
                "gateway.allow_non_loopback=true in config. "
                "This exposes the gateway to the network — ensure phantom token + HMAC "
                "are properly configured."
            )
        else:
            logger.warning(
                "non_loopback_binding",
                host=settings.gateway.host,
                msg="Gateway bound to non-loopback address — ensure network security is in place",
            )

    # 5. Session mode validation
    valid_modes = {"redis", "token_encoded"}
    if settings.gateway.session_mode not in valid_modes:
        errors.append(
            f"Invalid session_mode '{settings.gateway.session_mode}'. "
            f"Must be one of: {', '.join(sorted(valid_modes))}"
        )

    if errors:
        for err in errors:
            logger.error("config_validation_failed", error=err)
        raise ConfigValidationError(
            "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    logger.info("config_validation_passed")
