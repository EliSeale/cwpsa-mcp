"""
Idempotency store for cw_create — deduplicates retries across replicas (§8.1).

In production (multi-replica Container Apps): Redis-backed with short TTL.
In development / single-replica: in-process dict fallback.

An idempotency key maps to a stored result so a retry with the same key
returns the cached result instead of issuing a second POST to ConnectWise.

TODO Phase 2: wire Redis client from REDIS_URL and implement the full store.
Current implementation: in-process dict (single-replica safe only).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# In-process fallback store: {key: (result, expiry_monotonic)}
_store: dict[str, tuple[Any, float]] = {}
_lock = asyncio.Lock()

# TTL for idempotency records (5 minutes — covers agent retry windows)
IDEMPOTENCY_TTL = 300.0


async def get(key: str) -> Any | None:
    """Return a stored result for key, or None if not found / expired."""
    async with _lock:
        entry = _store.get(key)
        if entry and time.monotonic() < entry[1]:
            log.debug("[idempotency] cache hit for key %s", key[:16])
            return entry[0]
        if entry:
            del _store[key]
    return None


async def set(key: str, result: Any, ttl: float = IDEMPOTENCY_TTL) -> None:
    """Store a result under key with a TTL."""
    async with _lock:
        _store[key] = (result, time.monotonic() + ttl)
        log.debug("[idempotency] stored result for key %s (ttl=%.0fs)", key[:16], ttl)


async def clear_expired() -> int:
    """Remove expired entries.  Returns the number removed."""
    now = time.monotonic()
    async with _lock:
        expired = [k for k, (_, exp) in _store.items() if now >= exp]
        for k in expired:
            del _store[k]
    return len(expired)
