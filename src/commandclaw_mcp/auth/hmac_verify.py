"""HMAC-SHA256 canonical signing, nonce cache, and timestamp verification.

Mandatory on every request. Canonical string format:
    METHOD\nPATH\nTIMESTAMP\nNONCE\nBODY_HASH

Headers: X-Phantom-Token, X-Timestamp, X-Signature, X-Nonce
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import OrderedDict

import structlog

from commandclaw_mcp.observability.metrics import validation_failures_total

logger = structlog.get_logger()

# Timestamp tolerance: 300 seconds (5 minutes), matching the overlap window
TIMESTAMP_TOLERANCE_SECONDS = 300

# Maximum nonce cache size before eviction
_MAX_NONCE_CACHE_SIZE = 100_000


class NonceCache:
    """OrderedDict-based nonce cache with TTL eviction to prevent replay attacks."""

    def __init__(self, ttl_seconds: int = TIMESTAMP_TOLERANCE_SECONDS) -> None:
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl_seconds

    def check_and_store(self, nonce: str) -> bool:
        """Return True if the nonce is fresh (not seen before). Stores it if fresh."""
        self._evict_expired()

        if nonce in self._cache:
            return False

        self._cache[nonce] = time.time()
        return True

    def _evict_expired(self) -> None:
        """Remove nonces older than TTL from the front of the ordered dict."""
        cutoff = time.time() - self._ttl
        while self._cache:
            oldest_nonce, oldest_time = next(iter(self._cache.items()))
            if oldest_time < cutoff:
                self._cache.pop(oldest_nonce)
            else:
                break

        # Hard cap to prevent unbounded growth
        while len(self._cache) > _MAX_NONCE_CACHE_SIZE:
            self._cache.popitem(last=False)


def build_canonical_string(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    """Build the canonical string for HMAC signing.

    Format: METHOD\nPATH\nTIMESTAMP\nNONCE\nBODY_HASH
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{timestamp}\n{nonce}\n{body_hash}"


def sign_request(
    canonical_string: str,
    hmac_key: str,
) -> str:
    """Produce an HMAC-SHA256 signature of the canonical string."""
    return hmac.new(
        hmac_key.encode("utf-8"),
        canonical_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class HMACVerificationError(Exception):
    """Raised when HMAC verification fails for any reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class HMACVerifier:
    """Stateful HMAC verifier with nonce cache and timestamp checking."""

    def __init__(self, timestamp_tolerance: int = TIMESTAMP_TOLERANCE_SECONDS) -> None:
        self._nonce_cache = NonceCache(ttl_seconds=timestamp_tolerance)
        self._timestamp_tolerance = timestamp_tolerance

    def verify(
        self,
        *,
        method: str,
        path: str,
        timestamp: str,
        nonce: str,
        body: bytes,
        signature: str,
        hmac_key: str,
    ) -> None:
        """Verify an HMAC-signed request. Raises HMACVerificationError on failure.

        Checks in order:
        1. Timestamp freshness (< 5 min drift)
        2. Nonce uniqueness (reject replays)
        3. HMAC signature (constant-time comparison)
        """
        # 1. Timestamp freshness
        try:
            request_time = float(timestamp)
        except ValueError as exc:
            validation_failures_total.labels(reason="expired").inc()
            raise HMACVerificationError("Invalid timestamp format") from exc

        drift = abs(time.time() - request_time)
        if drift > self._timestamp_tolerance:
            validation_failures_total.labels(reason="expired").inc()
            logger.warning(
                "hmac_timestamp_expired",
                drift_seconds=round(drift, 1),
                tolerance=self._timestamp_tolerance,
            )
            raise HMACVerificationError(
                f"Timestamp drift {drift:.0f}s exceeds tolerance {self._timestamp_tolerance}s"
            )

        # 2. Nonce uniqueness
        if not self._nonce_cache.check_and_store(nonce):
            validation_failures_total.labels(reason="replay").inc()
            logger.warning("hmac_nonce_replay", nonce=nonce[:8])
            raise HMACVerificationError("Nonce already used (replay detected)")

        # 3. HMAC signature — constant-time comparison via hmac.compare_digest
        canonical = build_canonical_string(method, path, timestamp, nonce, body)
        expected = sign_request(canonical, hmac_key)

        if not hmac.compare_digest(signature, expected):
            validation_failures_total.labels(reason="invalid_hmac").inc()
            logger.warning("hmac_signature_invalid")
            raise HMACVerificationError("HMAC signature mismatch")
