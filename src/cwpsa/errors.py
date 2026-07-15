"""
Canonical error envelope (§7.1 of the architecture spec).

Every tool returns errors in this one structured shape — Problem-Details-style
(RFC 9457 in spirit) — so the agent reacts consistently instead of parsing prose.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

ErrorCode = Literal[
    "validation_error",       # §7 validator caught bad field/op/value
    "ambiguous_reference",    # cw_resolve returned multiple candidates
    "not_authorized",         # PEP denied the call
    "identity_unmapped",      # no active Office365-linked CW member for this Entra identity (§10.6)
    "impersonation_unavailable",  # per-user token mint failed (§10.6); deny all data
    "not_found",              # resource does not exist (distinguished from auth)
    "version_conflict",       # optimistic concurrency check failed (§8.1)
    "rate_limited",           # 429 from ConnectWise or internal quota
    "quota_exceeded",         # edge self-protection (§8.3): per-principal or global budget
    "upstream_unavailable",   # circuit breaker open (§8.2)
    "upstream_error",         # ConnectWise 4xx/5xx (sanitized)
    "write_disabled",         # CW_WRITES_DISABLED kill-switch active
]


class ErrorDetail(BaseModel):
    """Code-specific, model-actionable payload inside the envelope."""

    # validation_error
    suggestions: list[str] | None = None
    allowed_values: list[str] | None = None

    # ambiguous_reference
    candidates: list[dict[str, Any]] | None = None

    # version_conflict — current record state so the agent can redecide
    current_version: str | None = None
    current_record: dict[str, Any] | None = None

    # rate_limited / upstream_unavailable / quota_exceeded
    retry_after: int | None = None

    # identity_unmapped / impersonation_unavailable
    upn: str | None = None
    remediation: str | None = None

    # quota_exceeded
    limit_type: str | None = None  # "per_principal" | "global" | "session"


class MCPError(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    retry_after: int | None = None
    details: ErrorDetail | None = None


class ErrorEnvelope(BaseModel):
    """Top-level error response returned by every tool on failure."""

    error: MCPError


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def validation_error(
    message: str,
    suggestions: list[str] | None = None,
    allowed_values: list[str] | None = None,
) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(
            code="validation_error",
            message=message,
            retryable=False,
            details=ErrorDetail(suggestions=suggestions, allowed_values=allowed_values),
        )
    )


def ambiguous_reference(message: str, candidates: list[dict[str, Any]]) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(
            code="ambiguous_reference",
            message=message,
            retryable=False,
            details=ErrorDetail(candidates=candidates),
        )
    )


def not_authorized(message: str) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(code="not_authorized", message=message, retryable=False)
    )


def not_found(message: str) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(code="not_found", message=message, retryable=False)
    )


def version_conflict(
    message: str,
    current_version: str | None = None,
    current_record: dict[str, Any] | None = None,
) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(
            code="version_conflict",
            message=message,
            retryable=False,
            details=ErrorDetail(
                current_version=current_version, current_record=current_record
            ),
        )
    )


def rate_limited(retry_after: int) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(
            code="rate_limited",
            message=f"Rate limited by ConnectWise. Retry after {retry_after}s.",
            retryable=True,
            retry_after=retry_after,
            details=ErrorDetail(retry_after=retry_after),
        )
    )


def upstream_unavailable(retry_after: int | None = None) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(
            code="upstream_unavailable",
            message="ConnectWise temporarily unavailable (circuit open). Try again shortly.",
            retryable=True,
            retry_after=retry_after,
        )
    )


def upstream_error(message: str) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(code="upstream_error", message=message, retryable=False)
    )


def write_disabled() -> ErrorEnvelope:
    return ErrorEnvelope(
        error=MCPError(
            code="write_disabled",
            message="Mutation tools are currently disabled (CW_WRITES_DISABLED=1).",
            retryable=False,
        )
    )


def identity_unmapped(upn: str) -> ErrorEnvelope:
    """No active ConnectWise member is linked to this Entra identity (§10.6)."""
    return ErrorEnvelope(
        error=MCPError(
            code="identity_unmapped",
            message=(
                f"No active ConnectWise member is linked to this identity ({upn}). "
                "An administrator must add an active Office365 link on the member record "
                "before this account can access any ConnectWise data."
            ),
            retryable=False,
            details=ErrorDetail(upn=upn, remediation="link_office365_on_member"),
        )
    )


def impersonation_unavailable(reason: str, upn: str | None = None) -> ErrorEnvelope:
    """Per-user token mint failed — deny all data (fail-closed, §10.6)."""
    return ErrorEnvelope(
        error=MCPError(
            code="impersonation_unavailable",
            message=f"Per-user ConnectWise access cannot be established: {reason}",
            retryable=False,
            details=ErrorDetail(upn=upn, remediation="contact_administrator"),
        )
    )


def quota_exceeded(
    message: str,
    retry_after: int,
    limit_type: str = "per_principal",
) -> ErrorEnvelope:
    """Edge self-protection quota hit (§8.3) — retryable after retry_after seconds."""
    return ErrorEnvelope(
        error=MCPError(
            code="quota_exceeded",
            message=message,
            retryable=True,
            retry_after=retry_after,
            details=ErrorDetail(retry_after=retry_after, limit_type=limit_type),
        )
    )
