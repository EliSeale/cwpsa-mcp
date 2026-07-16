"""
ConnectWise HTTP client with the four resilience patterns (§8.2):

  request → [rate limit] → [bulkhead] → [circuit breaker] → [retry] → [timeout] → ConnectWise
              reject if      reject if    reject if open      retry on   per-attempt
              local quota    at capacity  (fast fail)         429/5xx    deadline

Components:
  1. Timeout        — httpx per-request timeout (30 s connect + read)
  2. Circuit breaker — pure-asyncio (replaces pybreaker which requires tornado);
                       trips after 5 consecutive 5xx/network failures;
                       half-open after 60 s; excluded: 4xx (client error) + 429
  3. Bulkhead       — anyio.CapacityLimiter; max 20 concurrent outbound calls
  4. Retry          — tenacity; up to 3 attempts; Retry-After-aware backoff + jitter;
                       idempotent verbs only (POST is never auto-retried — §8.1)
  Rate limit        — conservative in-process sliding window (~900/min vs CW's 1000/min)
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import time
from typing import Any

import anyio
import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from cwpsa import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-request credential context (set by token broker before each CW call, §10.6)
# ---------------------------------------------------------------------------
# Holds the minted member key pair for the current request's authenticated user.
# The PEP middleware resolves the Entra principal, mints (or retrieves cached) CW
# member keys via the broker, and stores them here before any tool executes.
# _execute() reads these for every outbound CW call.

_request_auth: contextvars.ContextVar[httpx.BasicAuth | None] = contextvars.ContextVar(
    "cw_request_auth", default=None
)
_request_user_type: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cw_request_user_type", default="member"
)


def set_request_credentials(
    auth: httpx.BasicAuth, user_type: str = "member"
) -> None:
    """Set per-request CW credentials (called by the token broker after mint, §10.6)."""
    _request_auth.set(auth)
    _request_user_type.set(user_type)


def clear_request_credentials() -> None:
    """Clear per-request credentials (e.g. at end of middleware scope)."""
    _request_auth.set(None)
    _request_user_type.set("member")

# ---------------------------------------------------------------------------
# Domain exceptions (used to steer the circuit breaker)
# ---------------------------------------------------------------------------

class _CWUnavailableError(Exception):
    """CW server error (5xx) or network failure — counts toward circuit breaker trips."""


class _RateLimitError(Exception):
    """CW 429 rate-limit response — excluded from circuit breaker failure counting."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited — retry after {retry_after}s")


# ---------------------------------------------------------------------------
# 1. Circuit breaker — pure asyncio, no tornado dependency
# ---------------------------------------------------------------------------
# Trips after 5 consecutive failures (5xx / network).
# 4xx (client errors) and 429 are EXCLUDED — they don't count as CW failures.


class CircuitBreakerError(Exception):
    """Raised when the circuit is OPEN (fast-fail path)."""


class _AsyncCircuitBreaker:
    """Minimal async-native circuit breaker (CLOSED → OPEN → HALF-OPEN → CLOSED).

    Excluded exception types are re-raised but do NOT increment the failure counter
    or contribute to tripping the breaker (e.g. 4xx client errors, 429 rate-limit).
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"

    def __init__(
        self,
        fail_max: int = 5,
        reset_timeout: float = 60.0,
        exclude: list | None = None,
        name: str = "circuit",
    ) -> None:
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self._exclude: list = list(exclude or [])
        self.name = name
        self._state = self.CLOSED
        self._failures = 0
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()

    @property
    def current_state(self) -> str:
        if self._state == self.OPEN:
            if time.monotonic() - self._last_failure_time >= self.reset_timeout:
                return self.HALF_OPEN
        return self._state

    async def call_async(self, func, *args, **kwargs):
        """Call `func` through the breaker.  Raises CircuitBreakerError when OPEN."""
        state = self.current_state
        if state == self.OPEN:
            raise CircuitBreakerError(
                f"Circuit breaker '{self.name}' is OPEN — ConnectWise temporarily unavailable."
            )
        try:
            result = await func(*args, **kwargs)
            # Successful call in HALF-OPEN → close the breaker
            if state == self.HALF_OPEN:
                async with self._lock:
                    self._state = self.CLOSED
                    self._failures = 0
                log.info("[circuit] %s: recovered → closed", self.name)
            return result
        except Exception as exc:
            # Excluded exception types don't count as failures
            if any(isinstance(exc, cls) for cls in self._exclude):
                raise
            async with self._lock:
                self._failures += 1
                self._last_failure_time = time.monotonic()
                if self._failures >= self.fail_max:
                    self._state = self.OPEN
                    log.warning(
                        "[circuit] %s: OPEN after %d consecutive failures",
                        self.name, self._failures,
                    )
            raise


_breaker = _AsyncCircuitBreaker(
    fail_max=5,
    reset_timeout=60,
    exclude=[httpx.HTTPStatusError, _RateLimitError],
    name="connectwise",
)


def get_circuit_state() -> str:
    """Return the circuit breaker state: 'closed', 'open', or 'half-open'."""
    return str(_breaker.current_state)


# ---------------------------------------------------------------------------
# 2. Bulkhead — cap concurrent outbound calls
# ---------------------------------------------------------------------------
_BULKHEAD_CAPACITY = 20
_limiter: anyio.CapacityLimiter | None = None


def _get_limiter() -> anyio.CapacityLimiter:
    global _limiter
    if _limiter is None:
        _limiter = anyio.CapacityLimiter(_BULKHEAD_CAPACITY)
    return _limiter


# ---------------------------------------------------------------------------
# 3. Rate limiter — sliding window (~900 req/min, headroom below CW's 1000)
# ---------------------------------------------------------------------------
class _SlidingWindowRateLimiter:
    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._window_start = 0.0
        self._count = 0
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def check(self) -> None:
        async with self._get_lock():
            now = time.monotonic()
            if now - self._window_start >= 60.0:
                self._window_start = now
                self._count = 0
            self._count += 1
            if self._count > self._max:
                wait_secs = int(60.0 - (now - self._window_start)) + 1
                log.warning("[rate-limit] local quota reached — fast-fail for %ds", wait_secs)
                raise _RateLimitError(retry_after=wait_secs)


_rate_limiter = _SlidingWindowRateLimiter(max_per_minute=900)


# ---------------------------------------------------------------------------
# 4. Retry helpers (tenacity)
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    """Return True if the exception warrants a retry."""
    if isinstance(exc, CircuitBreakerError):
        return False  # circuit open — fast-fail, never retry
    if isinstance(exc, _RateLimitError):
        return True   # 429 — wait then retry
    if isinstance(exc, _CWUnavailableError):
        return True   # 5xx / network — exponential backoff
    return False       # 4xx (HTTPStatusError) — client error, don't retry


class _WaitRetryAfterOrExponential:
    """Tenacity wait strategy: Retry-After header on 429, exponential+jitter otherwise."""

    def __call__(self, retry_state: Any) -> float:
        exc = retry_state.outcome.exception()
        if isinstance(exc, _RateLimitError):
            return float(exc.retry_after)
        n = retry_state.attempt_number
        return min(120.0, (2.0 ** n) + random.uniform(0.0, 1.0))


# ---------------------------------------------------------------------------
# HTTP client singleton
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the shared async CW HTTP client (no baked-in auth — per-request only)."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.CW_BASE_URL,
            # No auth= here — credentials are minted per-user by the broker (§10.6)
            # and injected per-request via _request_auth context var.
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Core request wrapper — converts httpx responses into domain exceptions
# ---------------------------------------------------------------------------

async def _execute(
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    """Make a single HTTP request using the per-request minted credentials.

    Auth and x-cw-usertype are taken from the request context var set by the
    token broker (§10.6).  If no credentials are set (local stdio dev), the call
    proceeds without auth — only valid when the CW instance allows it or when
    testing against a permissive local stub.
    """
    client = get_client()
    fn = getattr(client, method)

    # Credentials set by the broker for this request
    auth = _request_auth.get()
    user_type = _request_user_type.get()

    # Local dev fallback: no per-request credentials (no Entra JWT in stdio mode).
    # If CW_DEV_UPN is set, mint member credentials for the dev's own account so
    # local stdio testing works identically to production auth.
    if auth is None:
        import asyncio
        from cwpsa import config as _cfg
        if _cfg.DEV_UPN:
            from cwpsa.cache.token_broker import (
                BrokerNotConfigured, IdentityUnmapped, MintFailed, get_broker,
            )
            try:
                creds = await get_broker().get_member_credentials(_cfg.DEV_UPN)
                auth = httpx.BasicAuth(
                    f"{_cfg.CW_COMPANY}+{creds.public_key}",
                    creds.private_key,
                )
                user_type = "member"
                set_request_credentials(auth, user_type="member")
            except (BrokerNotConfigured, IdentityUnmapped, MintFailed) as e:
                log.warning("[client] dev UPN mint failed (%s) — no auth for this call", e)
        else:
            log.warning(
                "[client] no per-request auth and CW_DEV_UPN not set — "
                "CW calls will be unauthenticated. Set CW_DEV_UPN in .env for local dev."
            )

    per_request_headers = config.cw_headers(user_type=user_type)

    try:
        resp: httpx.Response = await fn(
            path,
            auth=auth,
            headers=per_request_headers,
            **kwargs,
        )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
        raise _CWUnavailableError(f"Network error on {method.upper()} {path}: {exc}") from exc

    # 429 — rate limited by CW; excluded from circuit breaker
    if resp.status_code == 429:
        after = int(resp.headers.get("Retry-After", "30"))
        log.warning("[cw] 429 rate-limited — Retry-After=%ds", after)
        raise _RateLimitError(retry_after=after)

    # 404 on cloud often means bad credentials (§8)
    if resp.status_code == 404:
        log.warning(
            "[cw] 404 on %s %s — may be incorrect CompanyId/keys on cloud CW instances (§8)",
            method.upper(), path,
        )
        resp.raise_for_status()  # httpx.HTTPStatusError — excluded from breaker

    # 5xx — CW server-side failure; trips the circuit breaker
    if resp.status_code >= 500:
        raise _CWUnavailableError(
            f"ConnectWise server error {resp.status_code} on {method.upper()} {path}"
        )

    # Other 4xx — client errors; excluded from breaker
    if not resp.is_success:
        resp.raise_for_status()  # httpx.HTTPStatusError

    return resp


# ---------------------------------------------------------------------------
# Resilient request dispatcher
# ---------------------------------------------------------------------------

async def _resilient(
    method: str,
    path: str,
    is_idempotent: bool = True,
    **kwargs: Any,
) -> Any:
    """Execute a CW HTTP call through the full 4-pattern resilience stack.

    Args:
        method:        httpx method name — "get", "post", "patch", "delete".
        path:          API path, e.g. "/service/tickets".
        is_idempotent: True for GET/PATCH/PUT/DELETE (auto-retried on transient failure).
                       False for POST (never auto-retried — §8.1).
        **kwargs:      Passed to httpx method (params=, json=, etc.).
    """
    # 1. Rate limit (fast reject if local quota exceeded)
    try:
        await _rate_limiter.check()
    except _RateLimitError as exc:
        from cwpsa.errors import rate_limited
        raise ValueError(str(exc)) from exc  # caller handles via error envelope

    # 2. Bulkhead (reject fast if at capacity)
    limiter = _get_limiter()
    if limiter.available_tokens < 1:
        log.warning("[bulkhead] at capacity (%d/%d)", _BULKHEAD_CAPACITY, _BULKHEAD_CAPACITY)
        raise _CWUnavailableError("Server at capacity — too many concurrent CW requests")

    async with limiter:
        if is_idempotent:
            # 3. Retry → circuit breaker → actual call
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=_WaitRetryAfterOrExponential(),  # type: ignore[arg-type]
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    resp = await _breaker.call_async(_execute, method, path, **kwargs)
                    return resp.json()
        else:
            # 3. Circuit breaker only — POST is never auto-retried (§8.1)
            resp = await _breaker.call_async(_execute, method, path, **kwargs)
            return resp.json()


# ---------------------------------------------------------------------------
# Public API — used by tools and integration modules
# ---------------------------------------------------------------------------

async def cw_get(path: str, **params: Any) -> Any:
    """GET a ConnectWise endpoint.  Returns parsed JSON."""
    return await _resilient("get", path, is_idempotent=True, params=_clean(params))


async def cw_post(path: str, body: dict[str, Any]) -> Any:
    """POST to a ConnectWise endpoint.  Never auto-retried (§8.1)."""
    return await _resilient("post", path, is_idempotent=False, json=_omit_nulls(body))


async def cw_patch(path: str, operations: list[dict[str, Any]]) -> Any:
    """PATCH a CW entity using the ConnectWise patch dialect (§8)."""
    return await _resilient("patch", path, is_idempotent=True, json=operations)


async def cw_delete(path: str) -> None:
    """DELETE a CW entity."""
    await _resilient("delete", path, is_idempotent=True)


async def cw_search_post(entity_path: str, conditions: str, **extra: Any) -> Any:
    """POST /{entity}/search — used when GET URL would exceed ~10,000 chars (§6.1)."""
    body: dict[str, Any] = {"conditions": conditions}
    body.update(_clean(extra))
    return await _resilient(
        "post", f"{entity_path.rstrip('/')}/search",
        is_idempotent=False, json=body,
    )


async def cw_count(path: str, **params: Any) -> int:
    """Return the count at /{path}/count."""
    result = await cw_get(f"{path.rstrip('/')}/count", **params)
    if isinstance(result, dict):
        return int(result.get("count", 0))
    return int(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _omit_nulls(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}
