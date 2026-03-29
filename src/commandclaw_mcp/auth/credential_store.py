"""Fernet + Argon2id encrypted credential storage with self-describing JSON format."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.fernet import Fernet

from commandclaw_mcp.security.memory import SecureBytes, zero_bytes

logger = structlog.get_logger()

# Argon2id parameters matching VISION.md
_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_COST = 65536  # 64 MiB
_ARGON2_PARALLELISM = 1
_ARGON2_HASH_LEN = 32
_SALT_LEN = 16


@dataclass
class CredentialEntry:
    """A real credential mapping for an upstream MCP server."""

    real_credential: str
    upstream_url: str
    header_name: str = "Authorization"
    credential_format: str = "Bearer {}"
    expires_at: float | None = None
    # Encrypted envelope (self-describing JSON) for at-rest storage
    encrypted_envelope: str | None = None

    @property
    def is_expired(self) -> bool:
        """Check if the credential has exceeded its TTL."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def format_for_injection(self) -> str:
        """Format the credential for upstream injection (header value)."""
        return self.credential_format.format(self.real_credential)


@dataclass
class PhantomSession:
    """A phantom token session binding an agent to its credentials."""

    phantom_token: str
    hmac_key: str
    agent_id: str
    credentials: dict[str, CredentialEntry] = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float = 0.0


def _derive_key(seed: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible key using Argon2id."""
    raw = hash_secret_raw(
        secret=seed.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_ARGON2_HASH_LEN,
        type=Argon2Type.ID,
    )
    # Fernet requires 32 bytes url-safe base64 encoded
    return base64.urlsafe_b64encode(raw)


def encrypt_credential(plaintext: str, seed: str) -> str:
    """Encrypt a credential using Fernet + Argon2id. Returns self-describing JSON."""
    salt = os.urandom(_SALT_LEN)
    key = _derive_key(seed, salt)
    try:
        f = Fernet(key)
        token = f.encrypt(plaintext.encode("utf-8"))
    finally:
        # Zero the key material
        key_buf = bytearray(key)
        zero_bytes(key_buf)

    envelope: dict[str, Any] = {
        "kdf": "argon2id",
        "t": _ARGON2_TIME_COST,
        "m": _ARGON2_MEMORY_COST,
        "p": _ARGON2_PARALLELISM,
        "salt": base64.b64encode(salt).decode("ascii"),
        "token": token.decode("ascii"),
    }
    return json.dumps(envelope)


def decrypt_credential(envelope_json: str, seed: str) -> SecureBytes:
    """Decrypt a credential from self-describing JSON. Returns SecureBytes for zeroing."""
    envelope = json.loads(envelope_json)

    salt = base64.b64decode(envelope["salt"])
    key = _derive_key(seed, salt)
    try:
        f = Fernet(key)
        plaintext = f.decrypt(envelope["token"].encode("ascii"))
    finally:
        key_buf = bytearray(key)
        zero_bytes(key_buf)

    return SecureBytes(plaintext)


async def encrypt_credential_async(plaintext: str, seed: str) -> str:
    """Async wrapper — runs Argon2id KDF in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(encrypt_credential, plaintext, seed)


async def decrypt_credential_async(envelope_json: str, seed: str) -> SecureBytes:
    """Async wrapper — runs Argon2id KDF in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(decrypt_credential, envelope_json, seed)
