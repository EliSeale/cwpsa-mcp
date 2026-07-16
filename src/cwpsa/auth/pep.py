"""
Policy Enforcement Point (PEP) — FastMCP Middleware (§10.3).

Three-layer enforcement:
  1. Per-tool auth check via FastMCP's native `auth=` parameter (§4.5).
     Write/delete tools declare `auth=write_auth_check` at registration
     (requires config.ENTRA_WRITE_SCOPE, default "mcp.write").

  2. PEPMiddleware — FastMCP Middleware subclass wired via mcp.add_middleware().
     Intercepts every tool call for:
     - §8.3 edge quota check (per-principal rate + concurrency cap).
     - §10.6 identity resolution + credential minting (sets per-request auth).
     - Structured audit logging (tool, principal, arg shape, latency, status).
     - Global kill-switch enforcement (CW_WRITES_DISABLED).

  3. Token broker (§10.6) — resolves Entra UPN → CW member → minted 4-hr keys.
     Sets `_request_auth` context var on the integration client before any
     ConnectWise call executes.  Fail-closed: any mapping or mint failure
     denies all data with a structured error.

Auth flow (§10.2/§10.6):
  Entra JWT → AzureJWTVerifier → PEPMiddleware.on_call_tool:
    1. Extract UPN from Entra token claims.
    2. Broker: resolve UPN → CW member + mint/retrieve 4-hr keys.
    3. Set per-request credentials on integration client.
    4. Edge quota check (§8.3).
    5. Dispatch tool.
    6. Release quota slot.  Audit log.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp.server.auth import AuthContext, require_scopes
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from cwpsa.observability.audit import log_tool_call

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-tool auth checks (used as auth= parameter at tool registration)
# ---------------------------------------------------------------------------


def write_auth_check(ctx: AuthContext) -> bool:
    """Allow write tools when the token carries the configured write scope.

    Local / stdio mode (ctx.token is None) is allowed — no Entra auth there.
    In HTTP mode a validated token must include ``config.ENTRA_WRITE_SCOPE``
    (default "mcp.write"). Entra emits scopes bare in the `scp` claim, and
    FastMCP exposes them on ``token.scopes`` as bare names (e.g. "mcp.write"),
    which is what this compares against — the ``api://<client_id>/`` prefix is
    only used when the client *requests* the scope, never in the issued token.
    """
    if ctx.token is None:
        return True  # stdio / local dev — no auth enforced
    from cwpsa import config
    return config.ENTRA_WRITE_SCOPE in set(ctx.token.scopes or [])


def delete_auth_check(ctx: AuthContext) -> bool:
    """Allow delete tools. Phase 1: same scope as write (config.ENTRA_WRITE_SCOPE)."""
    return write_auth_check(ctx)


# ---------------------------------------------------------------------------
# PEPMiddleware — audit log + kill-switch
# ---------------------------------------------------------------------------


class PEPMiddleware(Middleware):
    """FastMCP middleware: audit logging, kill-switch, and §8.3 edge self-protection.

    Ordering per §8.3 spec:
      auth/PEP (this middleware) → edge quota check → outbound resilience → ConnectWise
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: Any,
    ) -> Any:
        tool_name = getattr(context.message, "name", None) or "unknown"

        principal = _extract_principal(context)
        upn = _extract_upn(context)
        arg_shape = _extract_arg_shape(context)

        # --- DEBUG probe (remove or gate once auth verified) ---
        log.warning("[pep] tool=%s principal=%s upn=%s has_claims=%s",
                    tool_name, principal, upn, bool(_claims()))

        # 1. Global write kill-switch (belt-and-suspenders over per-tool auth=)
        if _is_write_tool(tool_name):
            from cwpsa import config
            if config.WRITES_DISABLED:
                log.warning(
                    "[pep] write blocked by kill-switch: tool=%s principal=%s",
                    tool_name, principal,
                )
                log_tool_call(
                    tool=tool_name, principal=principal, arg_shape=arg_shape,
                    status="denied:kill_switch", latency_ms=0,
                )
                from cwpsa.errors import write_disabled
                return write_disabled()

        # 2. §10.6 Token broker — resolve Entra identity → CW member → minted keys.
        #    Fail-closed (§10.3/§10.6): in an authenticated (HTTP) request, ANY failure
        #    to establish per-user scoping denies all data. Only genuine unauthenticated
        #    local stdio (no token at all) is allowed to proceed without per-user creds.
        from cwpsa.cache.token_broker import (
            BrokerNotConfigured, IdentityUnmapped, MintFailed, get_broker,
        )
        from cwpsa.integration.client import set_request_credentials
        import httpx
        from cwpsa import config as _cfg

        authenticated = bool(_claims())  # a validated Entra token is present on this request

        if upn:
            try:
                creds = await get_broker().get_member_credentials(upn)
                member_auth = httpx.BasicAuth(
                    f"{_cfg.CW_COMPANY}+{creds.public_key}",
                    creds.private_key,
                )
                set_request_credentials(member_auth, user_type="member")
                log.debug("[pep] credentials set for UPN '%s' (member %s)", upn, creds.member_identifier)
            except BrokerNotConfigured:
                if authenticated:
                    log.error("[pep] broker not configured on an authenticated request — denying")
                    log_tool_call(
                        tool=tool_name, principal=principal, arg_shape=arg_shape,
                        status="denied:impersonation_unavailable", latency_ms=0,
                    )
                    from cwpsa.errors import impersonation_unavailable
                    return impersonation_unavailable("token broker not configured")
                # else: genuine local stdio dev without integrator creds — allowed
                log.debug("[pep] broker not configured — local stdio, proceeding without per-user creds")
            except IdentityUnmapped as exc:
                log_tool_call(
                    tool=tool_name, principal=principal, arg_shape=arg_shape,
                    status="denied:identity_unmapped", latency_ms=0,
                )
                from cwpsa.errors import identity_unmapped
                return identity_unmapped(exc.upn)
            except MintFailed as exc:
                log.error("[pep] mint failed for UPN '%s': %s", upn, exc.reason)
                log_tool_call(
                    tool=tool_name, principal=principal, arg_shape=arg_shape,
                    status="denied:impersonation_unavailable", latency_ms=0,
                )
                from cwpsa.errors import impersonation_unavailable
                return impersonation_unavailable(exc.reason)
        elif authenticated:
            # Valid token but no UPN claim → app-only / client-credentials token.
            # There is no user to impersonate; fail closed rather than run unscoped.
            log.warning("[pep] authenticated request with no UPN claim (app-only token) — denying")
            log_tool_call(
                tool=tool_name, principal=principal, arg_shape=arg_shape,
                status="denied:identity_unmapped", latency_ms=0,
            )
            from cwpsa.errors import identity_unmapped
            return identity_unmapped(None)
        # else: no token at all AND not authenticated → genuine local stdio, allowed.

        # 3. §8.3 Edge self-protection — check and acquire quota slot
        from cwpsa.cache.edge_quota import QuotaExceeded, check_and_acquire, release
        from cwpsa.errors import quota_exceeded as _quota_exceeded
        try:
            await check_and_acquire(principal)
        except QuotaExceeded as exc:
            log.warning(
                "[pep] quota_exceeded: tool=%s principal=%s limit=%s retry_after=%d",
                tool_name, principal, exc.limit_type, exc.retry_after,
            )
            log_tool_call(
                tool=tool_name, principal=principal, arg_shape=arg_shape,
                status=f"denied:quota_{exc.limit_type}", latency_ms=0,
                extra={"limit_type": exc.limit_type},
            )
            return _quota_exceeded(exc.message, exc.retry_after, exc.limit_type)

        start = time.monotonic()
        status = "ok"
        try:
            result = await call_next(context)
            status = _result_status(result)  # detect tool-returned error envelopes
            return result
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            release(principal)
            latency_ms = (time.monotonic() - start) * 1000
            log_tool_call(
                tool=tool_name,
                principal=principal,
                arg_shape=arg_shape,
                status=status,
                latency_ms=latency_ms,
                extra={"entity": _extract_entity_arg(context)},
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WRITE_TOOLS = frozenset([
    "cw_create", "cw_update", "cw_delete",
    "cw_create_ticket", "cw_update_ticket",
    "cw_log_time",
])


def _is_write_tool(name: str) -> bool:
    return name in _WRITE_TOOLS


def _result_status(result: Any) -> str:
    """Best-effort: map a tool result to an audit status.

    Tools return ErrorEnvelope on failure but catch their own exceptions, so
    call_next returns normally — without this, every failed tool logs "ok".
    Defensive: unknown shapes fall back to "ok".
    """
    try:
        from cwpsa.errors import ErrorEnvelope
        if isinstance(result, ErrorEnvelope):
            return f"error:{result.error.code}"
        # FastMCP may wrap the return in a ToolResult; inspect its structured content.
        sc = getattr(result, "structured_content", None)
        if isinstance(sc, dict) and isinstance(sc.get("error"), dict):
            return f"error:{sc['error'].get('code', 'unknown')}"
        if isinstance(result, dict) and isinstance(result.get("error"), dict):
            return f"error:{result['error'].get('code', 'unknown')}"
    except Exception:
        pass
    return "ok"


def _claims() -> dict:
    """Signature-verified JWT claims for the current request, or {} if no token.

    Reads the request-scoped AccessToken populated by AzureJWTVerifier. Never
    pokes FastMCP internals; never silently swallows without logging.
    """
    try:
        token = get_access_token()
    except Exception:  # no auth context (stdio) or dependency unavailable
        return {}
    if token is None:
        return {}
    claims = getattr(token, "claims", None)
    if claims:
        return claims
    # Fallback for versions that expose the raw JWT but not decoded claims.
    raw = getattr(token, "token", None)
    if raw:
        try:
            import jwt  # PyJWT
            return jwt.decode(raw, options={"verify_signature": False})
        except Exception:
            log.exception("[pep] failed to decode access token claims")
    return {}


def _extract_principal(context: MiddlewareContext) -> str:
    """Stable per-user principal id from the validated token (oid preferred)."""
    c = _claims()
    return c.get("oid") or c.get("sub") or c.get("azp") or "anonymous"


def _extract_upn(context: MiddlewareContext) -> str | None:
    """Entra UPN from the validated JWT, used by the broker for the office365.name join.

    Returns None only when there is genuinely no user identity in the token
    (no token = local stdio; token-without-upn = app-only, handled by the PEP).
    """
    c = _claims()
    return (
        c.get("upn")
        or c.get("preferred_username")
        or c.get("unique_name")
        or c.get("email")
    )


def _extract_arg_shape(context: MiddlewareContext) -> dict[str, str]:
    """Return argument shape (names + types) — no values logged (§12.3 PII rule)."""
    try:
        args: dict[str, Any] = getattr(context.message, "arguments", None) or {}
        return {k: type(v).__name__ for k, v in args.items()}
    except Exception:
        return {}


def _extract_entity_arg(context: MiddlewareContext) -> str | None:
    """Extract the 'entity' argument if present (safe to log — it's a path, not data)."""
    try:
        args = getattr(context.message, "arguments", None) or {}
        return args.get("entity")
    except Exception:
        return None