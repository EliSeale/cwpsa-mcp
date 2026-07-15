"""
Entra ID JWT verification — resource-server / token-verification mode (§10.2, §12.1).

FastMCP's AzureJWTVerifier validates:
  - issuer (the tenant)
  - audience (api://{client_id} — the confused-deputy defense)
  - signature (Entra JWKS — no app secret needed)
  - expiry
  - required scopes (MCP.Tools.Read / MCP.Tools.Write)

No Dynamic Client Registration — pre-registered clients only (§12.1).

Returns the configured auth_provider (or None for unauthenticated local stdio).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def build_auth_provider():  # type: ignore[return]
    """Build and return a FastMCP AzureJWTVerifier, or None if Entra is unconfigured."""
    from cwpsa.config import ENTRA_CLIENT_ID, ENTRA_REQUIRED_SCOPES, ENTRA_TENANT_ID

    if not (ENTRA_TENANT_ID and ENTRA_CLIENT_ID):
        log.warning(
            "[auth] Entra ID not configured (ENTRA_TENANT_ID / ENTRA_CLIENT_ID missing). "
            "Running UNAUTHENTICATED — do not expose this server on a public endpoint."
        )
        return None

    try:
        from fastmcp.server.auth.providers.azure import AzureJWTVerifier

        provider = AzureJWTVerifier(
            client_id=ENTRA_CLIENT_ID,
            tenant_id=ENTRA_TENANT_ID,
            required_scopes=ENTRA_REQUIRED_SCOPES,
        )
        log.info("[auth] Entra ID JWT verification enabled (tenant=%s).", ENTRA_TENANT_ID)
        return provider
    except ImportError:
        log.error(
            "[auth] fastmcp.server.auth.providers.azure not available. "
            "Upgrade fastmcp to >=3.x with Azure auth support."
        )
        return None
    except Exception as exc:
        log.error("[auth] Failed to initialize Entra auth provider: %s", exc)
        return None
