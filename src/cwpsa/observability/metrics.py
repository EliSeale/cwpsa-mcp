"""
OTel metrics (§12.3).

Per-tool latency histograms, error rates, cache hit ratios, circuit-breaker state,
and consolidation-feedback metrics (call counts, result sizes, has_more frequency).

TODO: implement metric instruments and register them with the MCP middleware.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_metrics() -> None:
    """Initialize OTel metric instruments.  No-op stub — implement alongside tracing."""
    log.debug("[otel] metrics setup: stub — implement in Phase 1 observability sprint.")
