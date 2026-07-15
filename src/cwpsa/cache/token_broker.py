"""
Credential broker — per-user ConnectWise token lifecycle (§10.4/§10.6).

Maps an Entra principal (UPN) to a ConnectWise member via the office365.name
join, then mints and caches per-user 4-hr impersonation tokens via the
Integrator Login.

Fail-closed invariant (§10.3/§10.6):
  Any failure in identity mapping or minting → deny all data.
  NEVER fall back to a shared or broader credential.

Mint flow (§10.4 two-hop sequence):
  Hop 1 — resolve:  GET /system/members?conditions=office365/name="<upn>"
                    in integrator context  (x-cw-usertype: integrator)
  Hop 2 — mint:     POST /system/members/{id}/tokens
                    body { "memberIdentifier": "<identifier>" }
                    in integrator context
  Result: { publicKey, privateKey }  — 4-hr lifetime, member-scoped

Cache:
  In-process dict (per-replica) for Phase 1.
  Phase 5: envelope-encrypt private keys and store in Redis with
           single-flight refresh lock (lock:cwtoken:{memberId}).

Secret handling: minted private keys are live CW credentials.
  Production: TLS in-transit + at-rest encryption via Key Vault-wrapped key.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Token lifetime is 4 hours; proactive refresh when < 45 min remain
_TOKEN_TTL_SECONDS = 4 * 3600
_REFRESH_THRESHOLD_SECONDS = 45 * 60


class BrokerNotConfigured(Exception):
    """Raised when integrator credentials are missing."""


class IdentityUnmapped(Exception):
    """Raised when no active CW member is linked to the Entra principal."""
    def __init__(self, upn: str) -> None:
        self.upn = upn
        super().__init__(
            f"No active ConnectWise member linked to '{upn}'. "
            "An administrator must add an active Office365 link on the member "
            "record before this account can access any ConnectWise data."
        )


class MintFailed(Exception):
    """Raised when the impersonation token mint fails."""
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class MemberCredentials:
    """Per-user ConnectWise credentials (minted 4-hr keys)."""
    member_id: int
    member_identifier: str
    public_key: str
    private_key: str
    expires_at: float   # monotonic timestamp


# ---------------------------------------------------------------------------
# In-process token cache — Phase 1 (Redis in Phase 5)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[MemberCredentials, asyncio.Lock]] = {}
_cache_lock = asyncio.Lock()


async def _get_or_create_member_slot(upn: str) -> tuple[MemberCredentials | None, asyncio.Lock]:
    """Return (cached_creds_or_None, per-member mint lock)."""
    async with _cache_lock:
        if upn not in _cache:
            _cache[upn] = (None, asyncio.Lock())  # type: ignore[assignment]
        creds, lock = _cache[upn]
        return creds, lock


async def _store(upn: str, creds: MemberCredentials) -> None:
    async with _cache_lock:
        _, lock = _cache.get(upn, (None, asyncio.Lock()))
        _cache[upn] = (creds, lock)


# ---------------------------------------------------------------------------
# Identity mapping — Entra UPN → CW member (§10.6)
# ---------------------------------------------------------------------------

async def resolve_member_by_upn(upn: str) -> dict[str, Any]:
    """Map an Entra UPN to a CW member via the office365.name join.

    Calls GET /system/members in integrator context so the lookup succeeds
    even when Model A is not active.

    Returns: { id, identifier, firstName, lastName }
    Raises IdentityUnmapped if no active linked member is found.
    """
    from cwpsa import config

    safe_upn = upn.replace('"', '\\"')
    rows = await _integrator_get(
        "/system/members",
        conditions=f'office365/name="{safe_upn}" and inactiveFlag=False',
        fields="id,identifier,firstName,lastName",
        pageSize=2,
    )
    if not rows:
        raise IdentityUnmapped(upn)
    return rows[0]


# ---------------------------------------------------------------------------
# Integrator-context HTTP helpers (bypass the per-request auth context var)
# ---------------------------------------------------------------------------

async def _integrator_get(path: str, **params: Any) -> Any:
    """GET using API member keys (Hop 1) — read access to /system/members for lookup."""
    from cwpsa import config

    async with httpx.AsyncClient(
        base_url=config.CW_BASE_URL,
        auth=config.member_auth(),
        headers=config.cw_headers(user_type="member"),
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    ) as client:
        resp = await client.get(path, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()


async def _mint_member_token(member_id: int, member_identifier: str) -> tuple[str, str]:
    """Mint a 4-hr impersonation token via the Integrator Login (§10.4 hop 2).

    Returns (publicKey, privateKey) for the minted member session.
    Raises MintFailed on any error.
    """
    from cwpsa import config

    url = f"/system/members/{member_id}/tokens"
    try:
        async with httpx.AsyncClient(
            base_url=config.CW_BASE_URL,
            auth=config.integrator_auth(),
            headers=config.cw_headers(user_type="integrator"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            resp = await client.post(
                url,
                json={"memberIdentifier": member_identifier},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise MintFailed(
            f"Impersonation token mint failed for member {member_identifier} "
            f"({exc.response.status_code}): {exc.response.text[:200]}"
        ) from exc
    except Exception as exc:
        raise MintFailed(f"Impersonation token mint failed: {exc}") from exc

    pub = data.get("publicKey") or data.get("public_key")
    priv = data.get("privateKey") or data.get("private_key")
    if not pub or not priv:
        raise MintFailed(
            f"Mint response missing publicKey/privateKey: {list(data.keys())}"
        )
    return pub, priv


# ---------------------------------------------------------------------------
# TokenBroker — the main entry point
# ---------------------------------------------------------------------------

class TokenBroker:
    """Per-user token broker (§10.6).

    Resolves Entra UPN → CW member → minted 4-hr keys, with in-process
    caching and single-flight mint (one mint per member per refresh cycle).
    """

    async def get_member_credentials(self, upn: str) -> MemberCredentials:
        """Return cached or freshly-minted credentials for the given Entra UPN.

        Raises:
            BrokerNotConfigured: integrator login not configured.
            IdentityUnmapped:    no active linked CW member for this UPN.
            MintFailed:          token mint failed.

        Any exception → caller must deny all data (fail-closed, §10.3/§10.6).
        """
        from cwpsa import config

        if not config.INTEGRATOR_USERNAME or not config.INTEGRATOR_PASSWORD:
            raise BrokerNotConfigured(
                "Integrator Login credentials (CWPSA-Integrator-Username / "
                "CWPSA-Integrator-Password) are not configured."
            )

        cached, lock = await _get_or_create_member_slot(upn)

        # Serve from cache if fresh
        if cached and time.monotonic() < (cached.expires_at - _REFRESH_THRESHOLD_SECONDS):
            return cached

        # Single-flight: only one coroutine mints per member at a time
        async with lock:
            # Re-check after acquiring lock (another waiter may have refreshed)
            cached, _ = await _get_or_create_member_slot(upn)
            if cached and time.monotonic() < (cached.expires_at - _REFRESH_THRESHOLD_SECONDS):
                return cached

            log.info("[broker] minting CW token for UPN '%s'", upn)
            member = await resolve_member_by_upn(upn)
            pub, priv = await _mint_member_token(member["id"], member["identifier"])

            creds = MemberCredentials(
                member_id=member["id"],
                member_identifier=member["identifier"],
                public_key=pub,
                private_key=priv,
                expires_at=time.monotonic() + _TOKEN_TTL_SECONDS,
            )
            await _store(upn, creds)
            log.info(
                "[broker] minted token for member '%s' (id=%d), expires in %.0f min",
                member["identifier"], member["id"], _TOKEN_TTL_SECONDS / 60,
            )
            return creds

    async def revoke(self, upn: str) -> None:
        """Remove a cached token for a UPN (e.g. on forced logout or CW 401)."""
        async with _cache_lock:
            _cache.pop(upn, None)
        log.info("[broker] revoked cached token for UPN '%s'", upn)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_broker: TokenBroker = TokenBroker()


def get_broker() -> TokenBroker:
    return _broker

