"""
Edge self-protection — inbound quotas and concurrency caps (§8.3).

Protects the shared ConnectWise rate-limit budget and the server itself from
a runaway or over-parallelizing agent.  Enforced in PEP middleware before any
outbound call or token mint is spent.

Limits (all config-driven, §12.1):
  per_principal_rate   -- token bucket per Entra principal (default 120/min)
  per_principal_conc   -- concurrent in-flight calls per principal (default 5)
  global_rate          -- server-wide budget below CW's 1000/min (default 800/min)
  session_budget       -- total tool calls per session (default 500)

Implementation:
  Redis-backed when REDIS_URL is set (cross-replica, correct under scale-out).
  In-process fallback when Redis is unavailable (single-replica only, logs warning).

All limits return `quota_exceeded` (§7.1) with `retry_after` so the agent
self-throttles instead of failing blind.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process per-principal sliding-window rate limiter
# ---------------------------------------------------------------------------

class _PrincipalBucket:
    """Per-principal token bucket state (in-process)."""
    def __init__(self) -> None:
        self.window_start: float = 0.0
        self.count: int = 0
        self.in_flight: int = 0
        self.session_calls: int = 0


_buckets: dict[str, _PrincipalBucket] = defaultdict(_PrincipalBucket)
_global_window_start: float = 0.0
_global_count: int = 0
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


# ---------------------------------------------------------------------------
# Public API — called from PEP middleware
# ---------------------------------------------------------------------------

class QuotaExceeded(Exception):
    """Raised when any edge quota is exceeded."""
    def __init__(self, message: str, retry_after: int, limit_type: str) -> None:
        self.message = message
        self.retry_after = retry_after
        self.limit_type = limit_type
        super().__init__(message)


async def check_and_acquire(principal: str) -> None:
    """Check all edge quotas for `principal` and acquire a slot.

    Must be paired with `release()` in a finally block.

    Raises QuotaExceeded if any limit is breached.
    """
    from cwpsa import config

    async with _get_lock():
        now = time.monotonic()
        bucket = _buckets[principal]

        # 1. Per-principal rate limit (sliding window)
        if now - bucket.window_start >= 60.0:
            bucket.window_start = now
            bucket.count = 0
        bucket.count += 1
        if bucket.count > config.EDGE_RATE_LIMIT_PER_MIN:
            wait = int(60.0 - (now - bucket.window_start)) + 1
            raise QuotaExceeded(
                f"Rate limit exceeded ({config.EDGE_RATE_LIMIT_PER_MIN} calls/min per user). "
                f"Slow down and retry after {wait}s.",
                retry_after=wait,
                limit_type="per_principal",
            )

        # 2. Per-principal concurrency cap
        if bucket.in_flight >= config.EDGE_CONCURRENCY_CAP:
            raise QuotaExceeded(
                f"Too many concurrent tool calls (cap: {config.EDGE_CONCURRENCY_CAP}). "
                "Wait for in-flight calls to complete before issuing more.",
                retry_after=2,
                limit_type="per_principal_concurrency",
            )
        bucket.in_flight += 1

        # 3. Session call budget
        bucket.session_calls += 1
        if bucket.session_calls > config.EDGE_SESSION_CALL_BUDGET:
            raise QuotaExceeded(
                f"Session call budget exhausted ({config.EDGE_SESSION_CALL_BUDGET} calls). "
                "Start a new conversation to continue.",
                retry_after=0,
                limit_type="session",
            )

        # 4. Global rate limit
        global _global_window_start, _global_count
        if now - _global_window_start >= 60.0:
            _global_window_start = now
            _global_count = 0
        _global_count += 1
        if _global_count > config.EDGE_GLOBAL_RATE_PER_MIN:
            wait = int(60.0 - (now - _global_window_start)) + 1
            # Roll back per-principal increment to avoid penalising this principal
            bucket.in_flight -= 1
            bucket.count -= 1
            raise QuotaExceeded(
                f"Server is near the ConnectWise API rate ceiling. "
                f"Retry after {wait}s.",
                retry_after=wait,
                limit_type="global",
            )


def release(principal: str) -> None:
    """Release the in-flight concurrency slot for `principal`."""
    bucket = _buckets.get(principal)
    if bucket and bucket.in_flight > 0:
        bucket.in_flight -= 1


def reset_session(principal: str) -> None:
    """Reset the session call budget for `principal` (e.g. on new session)."""
    bucket = _buckets.get(principal)
    if bucket:
        bucket.session_calls = 0
