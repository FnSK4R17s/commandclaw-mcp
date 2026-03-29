"""PBKDF2 + AES-256-GCM token-encoded sessions (Envoy AI Gateway pattern)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logger = structlog.get_logger()

_SALT_LEN = 16
_NONCE_LEN = 12
_KEY_LEN = 32
_DEFAULT_ITERATIONS = 100_000


class Capability:
    """9-bit capability bitmask per backend (3-char hex).

    Encodes which MCP capabilities a backend supports, per the Envoy AI Gateway
    pattern. Used in token-encoded session IDs for routing decisions.
    """

    TOOLS = 1 << 0                    # 0x001
    TOOLS_LIST_CHANGED = 1 << 1       # 0x002
    PROMPTS = 1 << 2                  # 0x004
    PROMPTS_LIST_CHANGED = 1 << 3     # 0x008
    LOGGING = 1 << 4                  # 0x010
    RESOURCES = 1 << 5               # 0x020
    RESOURCES_LIST_CHANGED = 1 << 6  # 0x040
    RESOURCES_SUBSCRIBE = 1 << 7     # 0x080
    COMPLETIONS = 1 << 8            # 0x100

    ALL = (1 << 9) - 1  # 0x1FF — all 9 capabilities

    @staticmethod
    def to_hex(caps: int) -> str:
        """Encode capabilities as 3-char hex string."""
        return f"{caps:03x}"

    @staticmethod
    def from_hex(hex_str: str) -> int:
        """Decode capabilities from 3-char hex string."""
        return int(hex_str, 16)


def _derive_key(seed: str, salt: bytes, iterations: int = _DEFAULT_ITERATIONS) -> bytes:
    """Derive a 256-bit key using PBKDF2-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(seed.encode("utf-8"))


def encrypt_session(
    data: dict[str, Any],
    seed: str,
    subject: str,
    iterations: int = _DEFAULT_ITERATIONS,
    capabilities: dict[str, int] | None = None,
) -> str:
    """Encrypt session data with subject binding. Returns base64(salt || nonce || ciphertext).

    capabilities: optional dict of {backend_name: capability_bitmask} for
    encoding per-backend MCP capabilities into the token.
    """
    # Bind subject into payload to prevent session hijacking
    payload: dict[str, Any] = {"sub": subject, "data": data}
    if capabilities:
        # Encode capabilities as 3-char hex per backend
        payload["caps"] = {k: Capability.to_hex(v) for k, v in capabilities.items()}
    plaintext = json.dumps(payload).encode("utf-8")

    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(seed, salt, iterations)

    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    # Wire format: salt || nonce || ciphertext
    token_bytes = salt + nonce + ciphertext
    return base64.urlsafe_b64encode(token_bytes).decode("ascii")


def decrypt_session(
    token: str,
    seed: str,
    expected_subject: str,
    iterations: int = _DEFAULT_ITERATIONS,
) -> dict[str, Any]:
    """Decrypt and validate a token-encoded session. Raises on failure."""
    raw = base64.urlsafe_b64decode(token.encode("ascii"))

    salt = raw[:_SALT_LEN]
    nonce = raw[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
    ciphertext = raw[_SALT_LEN + _NONCE_LEN :]

    key = _derive_key(seed, salt, iterations)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)

    payload = json.loads(plaintext.decode("utf-8"))

    # Validate subject binding
    if payload.get("sub") != expected_subject:
        raise ValueError("Subject mismatch — possible session hijacking attempt")

    return payload["data"]


def decrypt_session_with_capabilities(
    token: str,
    seed: str,
    expected_subject: str,
    iterations: int = _DEFAULT_ITERATIONS,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Decrypt and return both data and capabilities.

    Returns (data, capabilities_dict) where capabilities_dict maps
    backend names to their capability bitmasks.
    """
    raw = base64.urlsafe_b64decode(token.encode("ascii"))

    salt = raw[:_SALT_LEN]
    nonce = raw[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
    ciphertext = raw[_SALT_LEN + _NONCE_LEN :]

    key = _derive_key(seed, salt, iterations)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)

    payload = json.loads(plaintext.decode("utf-8"))

    if payload.get("sub") != expected_subject:
        raise ValueError("Subject mismatch — possible session hijacking attempt")

    caps_raw = payload.get("caps", {})
    capabilities = {k: Capability.from_hex(v) for k, v in caps_raw.items()}

    return payload["data"], capabilities


class FallbackEnabledSessionCrypto:
    """Token-encoded session crypto with zero-downtime seed rotation.

    Tries primary seed first; on decryption failure, falls back to secondary.
    Encryption always uses the primary seed.
    """

    def __init__(
        self,
        primary_seed: str,
        secondary_seed: str | None = None,
        iterations: int = _DEFAULT_ITERATIONS,
    ) -> None:
        self._primary = primary_seed
        self._secondary = secondary_seed
        self._iterations = iterations

    async def encrypt(
        self,
        data: dict[str, Any],
        subject: str,
        capabilities: dict[str, int] | None = None,
    ) -> str:
        """Encrypt session data (runs in thread)."""
        return await asyncio.to_thread(
            encrypt_session, data, self._primary, subject, self._iterations, capabilities
        )

    async def decrypt(self, token: str, expected_subject: str) -> dict[str, Any]:
        """Decrypt session data with fallback to secondary seed (runs in thread)."""
        try:
            return await asyncio.to_thread(
                decrypt_session, token, self._primary, expected_subject, self._iterations
            )
        except Exception:
            if self._secondary is None:
                raise
            logger.debug("primary_seed_failed_trying_secondary")
            return await asyncio.to_thread(
                decrypt_session, token, self._secondary, expected_subject, self._iterations
            )
