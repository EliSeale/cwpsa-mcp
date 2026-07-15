"""
Resolution engine — fuzzy name/alias → ConnectWise ID + canonical value (§4.1 cw_resolve).

Resolves companies, members, service boards, board-scoped statuses, priorities,
and generic reference types.  Uses the alias map (§5.3) for synonym normalization,
then exact → substring → fuzzy matching.

The resolution logic here is migrated and improved from the legacy Server.py resolver
tools.  cw_resolve (tools/tier1/resolve.py) delegates to these functions.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from cwpsa.integration.client import cw_get


# ---------------------------------------------------------------------------
# String normalization helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Lowercase, alphanumeric-only — so 'McDonald's' and 'McDonalds' compare equal."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match(
    rows: list[dict[str, Any]],
    query: str,
    key: str = "name",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Rank rows by match quality against query on `key`.

    Priority: exact → substring → fuzzy (cutoff 0.6).
    """
    q = (query or "").strip().lower()
    if not q:
        return rows[:limit]
    exact = [r for r in rows if str(r.get(key, "")).lower() == q]
    if exact:
        return exact
    sub = [r for r in rows if q in str(r.get(key, "")).lower()]
    if sub:
        return sub[:limit]
    close = set(
        difflib.get_close_matches(
            q, [str(r.get(key, "")).lower() for r in rows], n=limit, cutoff=0.6
        )
    )
    return [r for r in rows if str(r.get(key, "")).lower() in close]


# ---------------------------------------------------------------------------
# Company resolution
# ---------------------------------------------------------------------------

async def resolve_company(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Resolve a company name or acronym to [{id, identifier, name}].

    Searches both `name` and `identifier` fields.  Handles punctuation
    differences (e.g. "McDonalds" → "McDonald's") via a punctuation-stripped
    fallback retry.

    Use the returned `identifier` in conditions: company/identifier="MWH"
    or the `id` as: company/id=123.
    """
    safe = query.replace('"', "").strip()
    rows = await cw_get(
        "/company/companies",
        conditions=f'deletedFlag!=true and (name contains "{safe}" or identifier contains "{safe}")',
        fields="id,identifier,name",
        pageSize=50,
    )

    # Fallback: punctuation mismatch retry on a stripped token
    if not rows and safe.split():
        token = re.sub(r"s$", "", safe.split()[0])
        if token and token.lower() != safe.lower():
            rows = await cw_get(
                "/company/companies",
                conditions=f'deletedFlag!=true and (name contains "{token}" or identifier contains "{token}")',
                fields="id,identifier,name",
                pageSize=50,
            )

    nq = _norm(query)

    def _score(r: dict[str, Any]) -> float:
        ident = _norm(r.get("identifier", ""))
        name = _norm(r.get("name", ""))
        if nq and nq == ident:
            return 1.0
        cands = [c for c in (ident, name) if c]
        base = 0.9 if any(nq and (nq in c or c in nq) for c in cands) else 0.0
        fuzzy = max(
            (difflib.SequenceMatcher(None, nq, c).ratio() for c in cands), default=0.0
        )
        return max(base, fuzzy)

    return sorted(rows, key=_score, reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Member resolution
# ---------------------------------------------------------------------------

async def resolve_member(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Resolve a member (user/technician) name or login to [{id, name}].

    Accepts first name, last name, full name, or login identifier.
    Use the returned `id` in conditions: owner/id=7.
    """
    rows = await cw_get(
        "/system/members",
        fields="id,identifier,firstName,lastName",
        pageSize=1000,
    )
    q = query.strip().lower()
    scored: list[tuple[float, dict[str, Any]]] = []

    for m in rows:
        first = (m.get("firstName") or "").lower()
        last = (m.get("lastName") or "").lower()
        full = f"{first} {last}".strip()
        ident = (m.get("identifier") or "").lower()

        if not q:
            continue
        if q in {first, last, ident}:
            s = 1.0
        elif q in full or q in ident or first.startswith(q) or last.startswith(q):
            s = 0.85
        else:
            s = max(
                (
                    difflib.SequenceMatcher(None, q, h).ratio()
                    for h in (first, last, full, ident)
                    if h
                ),
                default=0.0,
            )
        if s >= 0.6:
            display_name = f"{m.get('firstName', '')} {m.get('lastName', '')}".strip()
            scored.append((s, {"id": m["id"], "name": display_name, "identifier": ident}))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


# ---------------------------------------------------------------------------
# Priority resolution
# ---------------------------------------------------------------------------

async def resolve_priority(name: str) -> list[dict[str, Any]]:
    """Resolve a priority phrase to the exact ConnectWise priority name + id.

    Use the exact returned `name` in conditions: priority/name="Priority 1".
    """
    rows = await cw_get("/service/priorities", fields="id,name", pageSize=100)
    return _match(rows, name)


# ---------------------------------------------------------------------------
# Board resolution
# ---------------------------------------------------------------------------

async def resolve_board(name: str) -> list[dict[str, Any]]:
    """Resolve a service board name to [{id, name}].

    Call this before resolving board-scoped statuses.
    """
    rows = await cw_get("/service/boards", fields="id,name", pageSize=200)
    return _match(rows, name)


# ---------------------------------------------------------------------------
# Board-scoped status resolution
# ---------------------------------------------------------------------------

async def resolve_board_status(
    board_query: str, status_query: str
) -> dict[str, Any] | list[dict[str, Any]]:
    """Resolve a status name within a specific service board.

    Statuses are board-specific in ConnectWise — always resolve the board first.
    Returns matching statuses [{id, name}], or an error dict if the board is
    ambiguous or not found.
    """
    boards = _match(
        await cw_get("/service/boards", fields="id,name", pageSize=200), board_query
    )
    if not boards:
        return {"error": f"No service board matches '{board_query}'."}
    if len(boards) > 1:
        return {
            "error": "Multiple boards match; ask the user which one they meant.",
            "boards": boards,
        }
    board_id = boards[0]["id"]
    statuses = await cw_get(
        f"/service/boards/{board_id}/statuses", fields="id,name", pageSize=200
    )
    return _match(statuses, status_query)


# ---------------------------------------------------------------------------
# Generic reference type resolution
# ---------------------------------------------------------------------------

_REFERENCE_ENDPOINTS: dict[str, str] = {
    "company": "/company/companies",
    "contact": "/company/contacts",
    "member": "/system/members",
    "board": "/service/boards",
    "priority": "/service/priorities",
    "type": "/service/types",
    "subtype": "/service/subtypes",
    "item": "/service/items",
    "status": "/service/statuses",
    "sla": "/service/SLAs",
    "agreement_type": "/finance/agreementTypes",
    "work_role": "/time/workRoles",
    "work_type": "/time/workTypes",
    "department": "/system/departments",
    "location": "/system/locations",
    "manufacturer": "/procurement/manufacturers",
    "configuration_type": "/company/configurations/types",
}


async def resolve_reference(
    reference_type: str, query: str, context: dict[str, Any] | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """Generic resolver for any reference type.

    `reference_type` is a key from the table above (e.g. "priority", "member").
    For board-scoped resolution (status), pass context={"board": "<board_name>"}.

    Returns ranked matches [{id, name, ...}] or an error dict.
    """
    if reference_type == "status" and context and context.get("board"):
        return await resolve_board_status(context["board"], query)

    endpoint = _REFERENCE_ENDPOINTS.get(reference_type.lower())
    if not endpoint:
        return {"error": f"Unknown reference type '{reference_type}'."}

    rows = await cw_get(endpoint, fields="id,identifier,name", pageSize=500)
    return _match(rows, query, key="name")
