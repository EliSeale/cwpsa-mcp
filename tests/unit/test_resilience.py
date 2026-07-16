"""
Unit tests — resilience stack (§8.2, §13.1).

Tests the pure-asyncio circuit breaker, rate limiter, retry logic,
and the domain exception → error envelope translation.
"""

from __future__ import annotations

import asyncio
import pytest
import httpx

from cwpsa.integration.client import (
    _CWUnavailableError,
    _RateLimitError,
    _SlidingWindowRateLimiter,
    _execute,
    _is_retryable,
    _breaker,
    CircuitBreakerError,
)


class TestDomainExceptions:
    def test_rate_limit_error_stores_retry_after(self):
        err = _RateLimitError(retry_after=42)
        assert err.retry_after == 42

    def test_cw_unavailable_is_exception(self):
        err = _CWUnavailableError("server down")
        assert isinstance(err, Exception)


class TestIsRetryable:
    def test_circuit_open_not_retryable(self):
        exc = CircuitBreakerError("open")
        assert _is_retryable(exc) is False

    def test_rate_limit_is_retryable(self):
        exc = _RateLimitError(retry_after=30)
        assert _is_retryable(exc) is True

    def test_cw_unavailable_is_retryable(self):
        exc = _CWUnavailableError("5xx")
        assert _is_retryable(exc) is True

    def test_http_4xx_not_retryable(self):
        req = httpx.Request("GET", "https://example.com")
        resp = httpx.Response(400, request=req)
        exc = httpx.HTTPStatusError("Bad Request", request=req, response=resp)
        assert _is_retryable(exc) is False

    def test_other_exceptions_not_retryable(self):
        assert _is_retryable(ValueError("bad")) is False


class TestCircuitBreakerConfig:
    def test_breaker_starts_closed(self):
        assert _breaker.current_state == "closed"

    def test_http_status_error_excluded(self):
        """4xx HTTPStatusError should NOT count toward the breaker failure threshold."""
        assert httpx.HTTPStatusError in _breaker._exclude

    def test_rate_limit_error_excluded(self):
        """429 _RateLimitError should NOT count toward the breaker failure threshold."""
        assert _RateLimitError in _breaker._exclude


class TestSlidingWindowRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_requests_within_limit(self):
        limiter = _SlidingWindowRateLimiter(max_per_minute=10)
        for _ in range(10):
            await limiter.check()  # should not raise

    @pytest.mark.asyncio
    async def test_raises_on_exceeding_limit(self):
        limiter = _SlidingWindowRateLimiter(max_per_minute=3)
        for _ in range(3):
            await limiter.check()
        with pytest.raises(_RateLimitError) as exc_info:
            await limiter.check()
        assert exc_info.value.retry_after > 0

    @pytest.mark.asyncio
    async def test_window_resets_after_60s(self):
        import time
        limiter = _SlidingWindowRateLimiter(max_per_minute=2)
        for _ in range(2):
            await limiter.check()
        # Simulate window reset
        limiter._window_start = time.monotonic() - 61.0
        limiter._count = 99
        await limiter.check()  # should not raise after window reset
