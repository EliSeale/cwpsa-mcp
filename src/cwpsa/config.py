"""
Configuration — secrets from Azure Key Vault + environment overrides.

Two modes:
  Production:   KEY_VAULT_URI (or KEY_VAULT_URL) set; DefaultAzureCredential resolves
                via Managed Identity + RBAC.  Only the vault URI is in the environment —
                no bootstrap secret.  Matches §12.1 / §12.2.
  Local dev:    KEY_VAULT_URI set + az login / VS Code credential, OR
                CW_LOCAL_SECRETS=1 to fall back to env-vars / .env for each secret.

Secret names in Key Vault (§12.1):
  cw-integratorusername-01-mcp  -- Integrator Login username  (x-cw-usertype: integrator)
  cw-integratorpassword-01-mcp  -- Integrator Login password
  cw-companyId-01-mcp           -- ConnectWise company identifier  (e.g. "mettle")
  cw-clientid-01-mcp            -- clientId header value (integration app-id)

Two-hop credential model (§10.4/§10.6):
  Hop 1 — member lookup:  API member keys (cw-publickey-01-mcp / cw-privatekey-01-mcp)
                           x-cw-usertype: member  — read access to /system/members
  Hop 2 — token mint:     Integrator Login (username/password)
                           x-cw-usertype: integrator  — only credential that can mint
  Subsequent calls:        minted per-user keys, x-cw-usertype: member

Per-user member keys are minted at runtime by the broker (§10.6) and are
never stored as long-lived Key Vault secrets.
"""

from __future__ import annotations

import os
from functools import lru_cache

import httpx
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Key Vault client
# ---------------------------------------------------------------------------
# Support both KEY_VAULT_URI (new spec §12.1) and KEY_VAULT_URL (legacy) as aliases.
_vault_url: str = (
    os.environ.get("KEY_VAULT_URI")
    or os.environ.get("KEY_VAULT_URL")
    or ""
)
_local_secrets: bool = os.getenv("CW_LOCAL_SECRETS", "0") == "1"

_credential = DefaultAzureCredential()
_kv: SecretClient | None = None

if _vault_url:
    _kv = SecretClient(vault_url=_vault_url, credential=_credential)


def get_secret(name: str, default: str | None = None) -> str:
    """Fetch a secret from Key Vault.

    Falls back to an environment variable with the same name (uppercased,
    dashes replaced by underscores) when CW_LOCAL_SECRETS=1.  Raises if the
    secret is required and cannot be resolved.
    """
    if _local_secrets or _kv is None:
        env_name = name.upper().replace("-", "_")
        value = os.getenv(env_name)
        if value is not None:
            return value
        if default is not None:
            return default
        raise RuntimeError(
            f"Secret '{name}' not found. Set {env_name} in env / .env, "
            "or configure KEY_VAULT_URL with a proper credential."
        )
    try:
        return _kv.get_secret(name).value  # type: ignore[return-value]
    except Exception:
        if default is not None:
            return default
        raise


# ---------------------------------------------------------------------------
# ConnectWise config
# ---------------------------------------------------------------------------
# CW_COMPANY and CW_CLIENT_ID come from Key Vault.
# Per-user credentials are minted at runtime by the broker (§10.6).
# ---------------------------------------------------------------------------
_cw_base: str = os.getenv("CW_BASE_URL", "https://connect.verveit.com")
if "/v4_6_release/apis/3.0" not in _cw_base:
    _cw_base += "/v4_6_release/apis/3.0"
CW_BASE_URL: str = _cw_base

CW_COMPANY: str = get_secret("cw-companyId-01-mcp", os.getenv("CW_COMPANY", ""))
CW_CLIENT_ID: str = get_secret("cw-clientid-01-mcp")
CW_API_VERSION: str = get_secret("CWPSA-api-version", "2022.1")

# ---------------------------------------------------------------------------
# API Member keys — used ONLY for the identity-mapping lookup (Hop 1).
# These are read-only member credentials that can query /system/members.
# Hop 2 (minting) uses the Integrator Login below.
# Subsequent per-user calls use the minted keys from the broker.
# ---------------------------------------------------------------------------
CW_LOOKUP_PUBLIC_KEY: str = get_secret("cw-publickey-01-mcp")
CW_LOOKUP_PRIVATE_KEY: str = get_secret("cw-privatekey-01-mcp")


def member_auth() -> httpx.BasicAuth:
    """BasicAuth for the member-lookup hop (Hop 1) using static API member keys."""
    return httpx.BasicAuth(
        f"{CW_COMPANY}+{CW_LOOKUP_PUBLIC_KEY}", CW_LOOKUP_PRIVATE_KEY
    )


# ---------------------------------------------------------------------------
# Integrator Login — used by the token broker (§10.6) to mint per-user keys.
# Required for Model C (per-user impersonation).
# ---------------------------------------------------------------------------
INTEGRATOR_USERNAME: str = get_secret("cw-integratorusername-01-mcp")
INTEGRATOR_PASSWORD: str = get_secret("cw-integratorpassword-01-mcp")


def cw_headers(*, user_type: str = "member") -> dict[str, str]:
    """Build per-request ConnectWise headers.

    `user_type` must be "member" (minted user keys) or "integrator"
    (integrator login, used only by the token broker for minting).
    Auth credentials are passed separately per-request — never baked in.
    """
    return {
        "clientId": CW_CLIENT_ID,
        "Accept": f"application/vnd.connectwise.com+json; version={CW_API_VERSION}",
        "Content-Type": "application/json",
        "x-cw-usertype": user_type,
    }


def integrator_auth() -> "httpx.BasicAuth":
    """Basic auth for calls made in integrator context (minting and member lookup)."""
    return httpx.BasicAuth(f"{CW_COMPANY}+{INTEGRATOR_USERNAME}", INTEGRATOR_PASSWORD)


# ---------------------------------------------------------------------------
# Model C / Integrator Login credentials (Phase 5 — token broker §10.4/§10.6)
# cw-companyId-01-mcp / cw-clientid-01-mcp are the canonical names for these values.
# CW_COMPANY and CW_CLIENT_ID already hold the equivalent values above.

# ---------------------------------------------------------------------------
# Entra ID / edge auth
# ---------------------------------------------------------------------------
ENTRA_TENANT_ID: str = get_secret("entra-tenantid-01-mcp", "")
ENTRA_CLIENT_ID: str = get_secret("entra-clientid-01-mcp", "")
_raw_scopes: str = get_secret("entra-requiredscopes-01-mcp", "")
ENTRA_REQUIRED_SCOPES: list[str] | None = (
    _raw_scopes.replace(",", " ").split() or None
) if _raw_scopes else None

# ---------------------------------------------------------------------------
# Misc feature flags
# ---------------------------------------------------------------------------
REGISTRY_PATH: str = os.getenv("CW_REGISTRY_PATH", "registry.json")
LOAD_VOCABULARY: bool = os.getenv("CW_LOAD_VOCABULARY", "1") != "0"
WRITES_DISABLED: bool = os.getenv("CW_WRITES_DISABLED", "0").lower() in ("1", "true", "yes", "on")
OKF_BUNDLE_PATH: str = os.getenv("OKF_BUNDLE_PATH", "business-knowledge")
REDIS_URL: str | None = os.getenv("REDIS_URL")

# Local dev impersonation: when running in stdio/unauthenticated mode, the broker
# mints a token for this UPN instead of deriving it from the Entra JWT.
# Set to your Verve email address in .env for local testing.
DEV_UPN: str = os.getenv("CW_DEV_UPN", "")

# Response-size governance defaults (§4.4)
DEFAULT_PAGE_SIZE: int = int(os.getenv("CW_DEFAULT_PAGE_SIZE", "25"))
MAX_PAGE_SIZE: int = 1000  # ConnectWise hard cap
INLINE_TOKEN_BUDGET: int = int(os.getenv("CW_INLINE_TOKEN_BUDGET", "8000"))

# Edge self-protection limits (§8.3) — per-principal, in-process defaults
# Override via env vars; Redis-backed enforcement wires in when REDIS_URL is set.
EDGE_RATE_LIMIT_PER_MIN: int = int(os.getenv("CW_EDGE_RATE_PER_MIN", "120"))   # per principal
EDGE_CONCURRENCY_CAP: int = int(os.getenv("CW_EDGE_CONCURRENCY", "5"))          # concurrent calls per principal
EDGE_GLOBAL_RATE_PER_MIN: int = int(os.getenv("CW_EDGE_GLOBAL_RATE", "800"))   # global (below CW's 1000/min)
EDGE_SESSION_CALL_BUDGET: int = int(os.getenv("CW_EDGE_SESSION_BUDGET", "500")) # max calls per session