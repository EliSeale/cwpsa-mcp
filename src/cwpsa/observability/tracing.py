"""
OpenTelemetry tracing setup (§12.3).

Configures OTLP → Azure Monitor / Application Insights.
Each MCP tool call is a span; ConnectWise HTTP calls are child spans.
Trace IDs are propagated to ConnectWise via a custom request header.

TODO: wire tracer into tool wrappers and integration/client.py.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def setup_tracing() -> None:
    """Initialize OTel tracing.  No-op if APPLICATIONINSIGHTS_CONNECTION_STRING is unset."""
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        log.debug("[otel] APPLICATIONINSIGHTS_CONNECTION_STRING not set — tracing disabled.")
        return

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(connection_string=conn_str)
        log.info("[otel] Azure Monitor tracing configured.")
    except ImportError:
        log.warning("[otel] azure-monitor-opentelemetry not installed — tracing disabled.")
    except Exception as exc:
        log.warning("[otel] Failed to configure tracing: %s", exc)
