"""
Structured audit log (§10.1, §12.3).

One structured log line per tool call: authenticated principal, tool, argument shape,
status, latency.  PII and record free text are NOT logged (source-side redaction).

Argument *shape* is logged (field names and types), not values, to prevent
ticket summaries / company names / notes from entering the telemetry backend.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("cwpsa.audit")


def log_tool_call(
    tool: str,
    principal: str | None,
    arg_shape: dict[str, str],
    status: str,
    latency_ms: float,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one structured audit log line.

    Args:
        tool:        Tool name, e.g. "cw_query".
        principal:   Entra object ID of the caller (from JWT sub/oid claim).
                     None for unauthenticated / local stdio.
        arg_shape:   {arg_name: type_name} — shapes only, no values.
        status:      "ok" | "error:<code>" | "denied".
        latency_ms:  Round-trip latency in milliseconds.
        extra:       Additional structured fields (entity, error code, etc.).
    """
    record = {
        "event": "tool_call",
        "tool": tool,
        "principal": principal or "anonymous",
        "arg_shape": arg_shape,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        **(extra or {}),
    }
    log.info(json.dumps(record))


class AuditTimer:
    """Context manager to time a tool call and emit the audit log on exit."""

    def __init__(self, tool: str, principal: str | None, arg_shape: dict[str, str]) -> None:
        self.tool = tool
        self.principal = principal
        self.arg_shape = arg_shape
        self._start = 0.0
        self.status = "ok"
        self.extra: dict[str, Any] = {}

    def __enter__(self) -> "AuditTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        latency_ms = (time.monotonic() - self._start) * 1000
        if exc_type is not None:
            self.status = f"error:{exc_type.__name__}"
        log_tool_call(
            tool=self.tool,
            principal=self.principal,
            arg_shape=self.arg_shape,
            status=self.status,
            latency_ms=latency_ms,
            extra=self.extra,
        )
