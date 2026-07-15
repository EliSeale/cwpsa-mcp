"""
cw_resolve — Tier 1 tool: fuzzy reference resolution (§4.1).

Dedicated resolver for names → ConnectWise IDs and exact values.
Handles company, member, board, board-scoped status, type, priority, and
generic reference types.  Uses the alias map (§5.3) for synonym seeding.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.resolution.engine import (
    resolve_board,
    resolve_board_status,
    resolve_company,
    resolve_member,
    resolve_priority,
    resolve_reference,
)


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
        }
    )
    async def cw_resolve(
        reference_type: str,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Resolve a fuzzy name or phrase to ConnectWise IDs and exact values.

        Always call cw_resolve before building a filter with names — ConnectWise
        filters require exact IDs or exact enum strings.

        Args:
            reference_type: What to resolve.  One of:
                "company"   — by name or acronym → {id, identifier, name}
                              Use: company/identifier="MWH" or company/id=123
                "member"    — by name or login → {id, name, identifier}
                              Use: owner/id=7
                "board"     — service board → {id, name}
                "status"    — board-scoped status (requires context.board) → {id, name}
                              Use: status/name="New"
                "priority"  — ticket priority → {id, name}
                              Use: priority/name="Priority 1"
                Any other ConnectWise reference type (e.g. "type", "subtype",
                "agreement_type", "work_role", "manufacturer") → {id, name}

            query:   The name/phrase to resolve.  May be fuzzy, partial, or an acronym.
                     Examples: "ACME", "john", "help desk", "high", "expired"

            context: Extra context for board-scoped resolution.
                     {"board": "<board_name>"} is required when reference_type="status".

        Returns:
            A list of ranked matches [{id, name, ...}].
            - Single match: use its id/name directly.
            - Multiple matches: present candidates and ask the user which they meant.
            - Error dict: {"error": "...", "boards": [...]} for disambiguation.

        Examples:
            cw_resolve("company", "ACME")
            cw_resolve("member", "John")
            cw_resolve("status", "New", {"board": "Help Desk"})
            cw_resolve("priority", "high")
        """
        rt = reference_type.lower().strip()

        if rt == "company":
            return await resolve_company(query)
        if rt == "member":
            return await resolve_member(query)
        if rt == "board":
            return await resolve_board(query)
        if rt == "status":
            board = (context or {}).get("board", "")
            if not board:
                return {"error": "context.board is required when resolving a status. "
                                  "Call cw_resolve('board', ...) first to find the board name."}
            return await resolve_board_status(board, query)
        if rt == "priority":
            return await resolve_priority(query)

        # Generic fallback
        return await resolve_reference(rt, query, context)
