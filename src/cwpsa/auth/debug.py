"""
Verbose request / auth debug logging (§10.2 troubleshooting).

Toggle with CW_DEBUG_AUTH (default OFF). Set CW_DEBUG_AUTH=1 to enable while
auth chain is verified — this logs identity metadata and should not run forever
in production.

For every incoming MCP message it logs:
  - method + path + the NAMES of all request headers (never their values)
  - whether an Authorization bearer is present (+ length + a short, non-reversible
    fingerprint so you can tell if the same token repeats)
  - the UNVERIFIED, decoded JWT payload claims that matter for identity debugging
    (aud, iss, tid, idtyp, scp, roles, appid/azp, upn/preferred_username/email, exp)
  - what get_access_token() resolved to AFTER the verifier ran (the ground truth
    for whether FastMCP attached an authenticated user)

Safety: never logs the raw token, header values, signatures, or secrets. The JWT
payload is decoded WITHOUT signature verification purely to show what the caller
sent — do not trust these values for authorization (the verifier does that).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any

from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

log = logging.getLogger("cwpsa.debug.auth")

DEBUG_AUTH: bool = os.getenv("CW_DEBUG_AUTH", "0").lower() in ("1", "true", "yes", "on")

# Claims worth seeing when diagnosing "anonymous / no UPN / app-only token".
_IDENTITY_CLAIMS = (
    "aud", "iss", "tid", "idtyp", "scp", "roles", "appid", "azp",
    "upn", "preferred_username", "unique_name", "email", "name", "oid", "sub",
    "iat", "exp", "ver",
)

# Headers a host/proxy might inject that are useful to see (never Authorization value).
_INTERESTING_HEADERS = (
    "x-ms-client-principal", "x-ms-client-principal-name", "x-ms-client-principal-id",
    "x-ms-token-aad-access-token", "x-forwarded-for", "x-forwarded-proto",
    "user-agent", "content-type", "mcp-session-id", "mcp-protocol-version",
)


def _fingerprint(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:12]


def _decode_jwt_payload_unverified(tok: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT verifying the signature (debug display only)."""
    try:
        parts = tok.split(".")
        if len(parts) < 2:
            return {"_error": "not a JWT (no '.' segments) — opaque token?"}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception as exc:  # noqa: BLE001
        return {"_decode_error": str(exc)}


def _log_incoming_request() -> None:
    try:
        req = get_http_request()
    except Exception:  # noqa: BLE001
        log.warning("[debug] no HTTP request in context (stdio transport?)")
        return

    hdrs = req.headers
    log.warning("[debug] %s %s", req.method, req.url.path)
    log.warning("[debug] header_names=%s", sorted(hdrs.keys()))
    for h in _INTERESTING_HEADERS:
        if h in hdrs:
            # These are non-secret; safe to log to help spot Easy-Auth / proxy injection.
            log.warning("[debug]   %s=%s", h, hdrs.get(h))

    auth = hdrs.get("authorization")
    if not auth:
        log.warning("[debug] >>> NO Authorization header — the caller sent NO bearer token. "
                    "This is why the request is anonymous. Fix is on the CALLER (Foundry) side.")
        return

    scheme, _, value = auth.partition(" ")
    log.warning("[debug] Authorization present: scheme=%r token_len=%d fp=%s",
                scheme, len(value), _fingerprint(value) if value else "-")

    if scheme.lower() != "bearer" or not value:
        log.warning("[debug] Authorization is not a Bearer token — unexpected scheme %r", scheme)
        return

    claims = _decode_jwt_payload_unverified(value)
    if "_error" in claims or "_decode_error" in claims:
        log.warning("[debug] could not decode token payload: %s", claims)
        return

    subset = {k: claims.get(k) for k in _IDENTITY_CLAIMS if k in claims}
    log.warning("[debug] token payload (UNVERIFIED) identity claims: %s", subset)

    idtyp = claims.get("idtyp")
    has_user = any(claims.get(k) for k in ("upn", "preferred_username", "unique_name", "email"))
    has_scp = "scp" in claims
    has_roles = "roles" in claims

    if has_user:
        log.warning("[debug] >>> USER (delegated) token — has upn/email. Identity mapping should work.")
    elif idtyp == "app" or (has_roles and not has_scp):
        log.warning("[debug] >>> APP-ONLY token (client-credentials): idtyp=%r, has scp=%s roles=%s. "
                    "There is NO user to impersonate. Foundry must use a DELEGATED / user OAuth flow, "
                    "not client credentials.", idtyp, has_scp, has_roles)
    else:
        log.warning("[debug] >>> token has neither a user claim nor a clear app marker — "
                    "inspect the full claim subset above.")

    # Audience sanity: the aud must match your MCP server's API app.
    log.warning("[debug] audience check: token aud=%r (must equal your ENTRA_CLIENT_ID / api://<client_id>)",
                claims.get("aud"))


def _log_validated_token() -> None:
    """What the verifier actually attached (ground truth, post-validation)."""
    try:
        tok = get_access_token()
    except Exception:  # noqa: BLE001
        tok = None
    if tok is None:
        log.warning("[debug] get_access_token() -> None : verifier attached NO authenticated user. "
                    "Either no bearer arrived, or it failed validation (aud/iss/expiry).")
        return
    claims = getattr(tok, "claims", None) or {}
    subset = {k: claims.get(k) for k in _IDENTITY_CLAIMS if k in claims}
    scopes = getattr(tok, "scopes", None)
    log.warning("[debug] get_access_token() OK : scopes=%s validated_claims=%s", scopes, subset)


class DebugMiddleware(Middleware):
    """Logs the incoming request + token on every MCP message. Add BEFORE PEPMiddleware."""

    async def on_message(self, context: MiddlewareContext, call_next: Any) -> Any:
        if DEBUG_AUTH:
            try:
                mtype = type(getattr(context, "message", None)).__name__
                log.warning("[debug] ===== on_message type=%s =====", mtype)
                _log_incoming_request()
                _log_validated_token()
            except Exception:  # noqa: BLE001
                log.exception("[debug] debug logging failed (non-fatal)")
        return await call_next(context)