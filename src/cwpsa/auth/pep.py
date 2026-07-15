"""
Policy Enforcement Point (PEP) — FastMCP Middleware (§10.3).

Three-layer enforcement:
  1. Per-tool auth check via FastMCP's native `auth=` parameter (§4.5).
     Write/delete tools declare `auth=write_auth_check` at registration.

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
from fastmcp.server.middleware import Middleware, MiddlewareContext

from cwpsa.observability.audit import log_tool_call

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-tool auth checks (used as auth= parameter at tool registration)
# ---------------------------------------------------------------------------


def write_auth_check(ctx: AuthContext) -> bool:
    """Allow write tools when the principal has MCP.Tools.Write scope.

    In stdio / local mode (ctx.token is None) all operations are allowed —
    local dev runs without Entra auth.  In HTTP mode a valid token with the
    write scope is required.
    """
    if ctx.token is None:
        return True  # stdio mode — no auth enforced
    return "MCP.Tools.Write" in set(ctx.token.scopes)


def delete_auth_check(ctx: AuthContext) -> bool:
    """Allow delete tools. Phase 1: same scope as write (MCP.Tools.Write)."""
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
        tool_name = "unknown"
        try:
            tool_name = context.message.params.name  # type: ignore[union-attr]
        except Exception:
            pass

        principal = _extract_principal(context)
        upn = _extract_upn(context)
        arg_shape = _extract_arg_shape(context)

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
        #    Fail-closed: unmapped identity or mint failure → deny all data.
        from cwpsa.cache.token_broker import (
            BrokerNotConfigured, IdentityUnmapped, MintFailed, get_broker,
        )
        from cwpsa.integration.client import set_request_credentials
        import httpx
        from cwpsa import config as _cfg

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
                # Local stdio dev without integrator credentials — proceed without auth.
                # In HTTP mode with Entra auth, this should never happen.
                log.debug("[pep] broker not configured — proceeding without per-user credentials (local dev)")
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


def _extract_principal(context: MiddlewareContext) -> str:
    """Get the authenticated principal ID (object ID or client_id) from the FastMCP context."""
    try:
        if context.fastmcp_context and context.fastmcp_context.client_id:
            return context.fastmcp_context.client_id
    except Exception:
        pass
    return "anonymous"


def _extract_upn(context: MiddlewareContext) -> str | None:
    """Extract the Entra UPN (upn or preferred_username claim) from the validated JWT.

    The UPN is used by the token broker to resolve the CW member identity (§10.6).
    Returns None in stdio/local mode where no JWT is present.
    """
    try:
        # FastMCP stores the validated AccessToken on the session; claims dict
        # comes from the JWT payload (already signature-verified by AzureJWTVerifier).
        token = getattr(context.fastmcp_context, "_session", None)
        if token is None:
            return None
        # Try to get claims from the FastMCP AccessToken
        access_token = getattr(token, "access_token", None) or getattr(token, "_access_token", None)
        if access_token and hasattr(access_token, "claims"):
            claims = access_token.claims
            return (
                claims.get("upn")
                or claims.get("preferred_username")
                or claims.get("unique_name")
                or claims.get("email")
            )
    except Exception:
        pass
    return None


def _extract_arg_shape(context: MiddlewareContext) -> dict[str, str]:
    """Return argument shape (names + types) — no values logged (§12.3 PII rule)."""
    try:
        args: dict[str, Any] = context.message.params.arguments or {}  # type: ignore
        return {k: type(v).__name__ for k, v in args.items()}
    except Exception:
        return {}


def _extract_entity_arg(context: MiddlewareContext) -> str | None:
    """Extract the 'entity' argument if present (safe to log — it's a path, not data)."""
    try:
        args = context.message.params.arguments or {}  # type: ignore
        return args.get("entity")
    except Exception:
        return None

