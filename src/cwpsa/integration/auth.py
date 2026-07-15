"""
CredentialProvider — abstracts downstream ConnectWise credentials (§10.4).

Three models, increasing fidelity:
  Model A: single shared least-privilege API member (v1 — ships first)
  Model B: tiered API members by role (Phase 2 — write safety)
  Model C: member impersonation per Entra user (Phase 5 — full attribution)

Tools never interact with credentials directly — they call get_client()
from integration/client.py which uses the active CredentialProvider.

For v1 (Model A) the provider just returns the static config credentials.
The abstraction is in place so Model B/C are a drop-in swap.
"""

from __future__ import annotations

import httpx
from cwpsa import config


class CredentialProvider:
    """Base credential provider interface."""

    async def get_auth(self, principal_id: str | None = None) -> httpx.BasicAuth:
        raise NotImplementedError

    async def get_headers(self, principal_id: str | None = None) -> dict[str, str]:
        raise NotImplementedError


class ModelAProvider(CredentialProvider):
    """Model A — single shared least-privilege API member (v1).

    All authorized users share one downstream CW identity.
    Per-user authorization is enforced by the PEP (auth/pep.py) alone.
    """

    async def get_auth(self, principal_id: str | None = None) -> httpx.BasicAuth:
        return config.CW_AUTH

    async def get_headers(self, principal_id: str | None = None) -> dict[str, str]:
        return config.CW_HEADERS


# ---------------------------------------------------------------------------
# Active provider (swap this to ModelB/C in later phases)
# ---------------------------------------------------------------------------
_provider: CredentialProvider = ModelAProvider()


def get_credential_provider() -> CredentialProvider:
    return _provider


def set_credential_provider(provider: CredentialProvider) -> None:
    """Override the active credential provider (used in tests and phased rollout)."""
    global _provider
    _provider = provider
