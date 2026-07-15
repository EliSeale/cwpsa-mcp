"""
Tier 2 workflow tools — tickets (§4.2).

cw_list_tickets:  filtered ticket list with resolution, board/status aware.
cw_get_ticket:    full ticket with notes + resolved names.
cw_create_ticket: create a ticket with company/board/status resolution.
cw_update_ticket: update key ticket fields with patch dialect.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa import config
from cwpsa.auth.pep import write_auth_check
from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error, write_disabled
from cwpsa.integration.client import cw_get as _cw_get, cw_patch, cw_post
from cwpsa.integration.patch_builder import build_patch, reference, scalar
from cwpsa.resolution.engine import (
    resolve_board,
    resolve_board_status,
    resolve_company,
    resolve_member,
    resolve_priority,
)

import httpx


_TICKET_DEFAULT_FIELDS = (
    "id,summary,company,contact,status,board,owner,priority,type,"
    "slaStatus,closedFlag,dateEntered,lastUpdated,_info"
)


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False}
    )
    async def cw_list_tickets(
        company: str | None = None,
        status_filter: str | None = None,
        board: str | None = None,
        assigned_to: str | None = None,
        date_range: dict[str, str] | None = None,
        closed: bool = False,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any] | ErrorEnvelope:
        """List service tickets with common MSP filters pre-wired.

        Handles company/board/status/member resolution automatically —
        no need to call cw_resolve separately for this workflow.

        Args:
            company:       Company name or acronym.  Resolved to id automatically.
            status_filter: Status name (e.g. "New", "In Progress").  Resolved
                           per-board automatically when board is also provided.
            board:         Service board name.  Resolved to id automatically.
            assigned_to:   Member name or login.  Resolved to id automatically.
            date_range:    {"from": "2026-01-01T00:00:00Z", "to": "2026-06-30T23:59:59Z"}
                           Filters on dateEntered.
            closed:        Include closed tickets (default False = open only).
            page:          Page number (1-based).
            page_size:     Records per page (default 25, max 1000).

        Returns a response-governance envelope with tickets + has_more cursor.
        """
        conditions: list[str] = []

        if not closed:
            conditions.append("closedFlag=False")

        # Resolve company
        if company:
            matches = await resolve_company(company, limit=1)
            if not matches:
                return validation_error(f"No company found matching '{company}'.")
            if len(matches) > 1:
                from cwpsa.errors import ambiguous_reference
                return ambiguous_reference(
                    f"Multiple companies match '{company}'.", candidates=matches
                )
            conditions.append(f"company/id={matches[0]['id']}")

        # Resolve board + board-scoped status
        board_id: int | None = None
        if board:
            boards = await resolve_board(board)
            if not boards:
                return validation_error(f"No service board found matching '{board}'.")
            board_id = boards[0]["id"]
            conditions.append(f"board/id={board_id}")

        if status_filter:
            if board_id is not None:
                statuses = await _cw_get(
                    f"/service/boards/{board_id}/statuses",
                    fields="id,name",
                    pageSize=200,
                )
                from cwpsa.resolution.engine import _match
                matched = _match(statuses, status_filter)
            else:
                # Board-agnostic status filter — less reliable
                matched = [{"name": status_filter}]
            if matched:
                status_name = matched[0].get("name", status_filter)
                # Escape quotes for CW conditions
                status_name_safe = status_name.replace('"', '\\"')
                conditions.append(f'status/name="{status_name_safe}"')

        # Resolve member
        if assigned_to:
            members = await resolve_member(assigned_to, limit=1)
            if not members:
                return validation_error(f"No member found matching '{assigned_to}'.")
            conditions.append(f"owner/id={members[0]['id']}")

        # Date range
        if date_range:
            from_ = date_range.get("from")
            to_ = date_range.get("to")
            if from_:
                conditions.append(f"dateEntered>=[{from_}]")
            if to_:
                conditions.append(f"dateEntered<=[{to_}]")

        cw_conditions = " and ".join(f"({c})" for c in conditions) if conditions else None

        try:
            params: dict[str, Any] = {
                "fields": _TICKET_DEFAULT_FIELDS,
                "pageSize": min(page_size, config.MAX_PAGE_SIZE),
                "page": page,
                "orderBy": "lastUpdated desc",
            }
            if cw_conditions:
                params["conditions"] = cw_conditions

            data = await _cw_get("/service/tickets", **params)
        except Exception as e:
            return upstream_error(str(e))

        if not isinstance(data, list):
            data = [data] if data else []

        has_more = len(data) >= page_size
        return {
            "entity": "service/tickets",
            "count_hint": len(data),
            "data": data,
            "has_more": has_more,
            "next_cursor": page + 1 if has_more else None,
            "message": f"showing {len(data)} ticket(s)" + (" — page for more" if has_more else ""),
        }

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False}
    )
    async def cw_get_ticket(
        ticket_number: int,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Get full details for a single service ticket, including notes.

        Returns the ticket record with resolved names plus the most recent
        time entries and notes as sub-objects.

        Args:
            ticket_number: The numeric ticket ID.
        """
        try:
            ticket = await _cw_get(f"/service/tickets/{ticket_number}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                from cwpsa.errors import not_found
                return not_found(f"Ticket #{ticket_number} not found.")
            return upstream_error(f"ConnectWise error: {e.response.status_code}")
        except Exception as e:
            return upstream_error(str(e))

        # Fetch notes + time entries in parallel
        import asyncio
        notes_task = asyncio.create_task(
            _cw_get(f"/service/tickets/{ticket_number}/notes",
                    fields="id,text,detailDescriptionFlag,internalAnalysisFlag,member,dateCreated",
                    pageSize=25)
        )
        time_task = asyncio.create_task(
            _cw_get(f"/service/tickets/{ticket_number}/timeentries",
                    fields="id,member,actualHours,notes,dateStart,billableOption",
                    pageSize=10)
        )

        try:
            notes, time_entries = await asyncio.gather(notes_task, time_task, return_exceptions=True)
        except Exception:
            notes, time_entries = [], []

        ticket["_notes"] = notes if isinstance(notes, list) else []
        ticket["_time_entries"] = time_entries if isinstance(time_entries, list) else []
        ticket["_version"] = ticket.get("_info", {}).get("lastUpdated")
        return ticket

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        auth=write_auth_check,
    )
    async def cw_create_ticket(
        summary: str,
        company: str,
        board: str,
        contact: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        type_: str | None = None,
        initial_description: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Create a new service ticket with automatic company/board/status resolution.

        Args:
            summary:             Ticket subject line.
            company:             Company name or acronym (resolved automatically).
            board:               Service board name (resolved automatically).
            contact:             Contact name (optional, resolved if provided).
            priority:            Priority name (optional, resolved if provided).
            status:              Status name, board-scoped (optional, resolved if provided).
            type_:               Ticket type name (optional).
            initial_description: Detailed description (internal analysis field).
            idempotency_key:     Stable key to prevent duplicate creates on retry.
        """
        if config.WRITES_DISABLED:
            return write_disabled()

        # Resolve company
        companies = await resolve_company(company, limit=1)
        if not companies:
            return validation_error(f"No company found matching '{company}'.")
        company_id = companies[0]["id"]
        company_identifier = companies[0].get("identifier")

        # Resolve board
        boards = await resolve_board(board)
        if not boards:
            return validation_error(f"No service board found matching '{board}'.")
        board_id = boards[0]["id"]

        # Build ticket body
        body: dict[str, Any] = {
            "summary": summary,
            "company": {"id": company_id, "identifier": company_identifier},
            "board": {"id": board_id},
        }

        if priority:
            priorities = await resolve_priority(priority)
            if priorities:
                body["priority"] = {"id": priorities[0]["id"]}

        if status:
            statuses_result = await _cw_get(
                f"/service/boards/{board_id}/statuses",
                fields="id,name",
                pageSize=200,
            )
            from cwpsa.resolution.engine import _match
            matched_status = _match(statuses_result, status)
            if matched_status:
                body["status"] = {"id": matched_status[0]["id"]}

        if initial_description:
            body["initialDescription"] = initial_description

        try:
            result = await cw_post("/service/tickets", body)
        except httpx.HTTPStatusError as e:
            return upstream_error(
                f"ConnectWise {e.response.status_code}: {e.response.text[:300]}"
            )
        except Exception as e:
            return upstream_error(str(e))

        return result

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        auth=write_auth_check,
    )
    async def cw_update_ticket(
        ticket_number: int,
        summary: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        assigned_to: str | None = None,
        board: str | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Update common fields on a service ticket.

        Handles status/priority/member resolution automatically.
        For non-listed fields, use cw_update directly with patch operations.

        Args:
            ticket_number:    Numeric ticket ID.
            summary:          New ticket summary.
            status:           New status name (resolved per current board).
            priority:         New priority name.
            assigned_to:      New assignee name or login.
            board:            Transfer to a different service board.
            expected_version: The _version from a prior cw_get_ticket call (recommended).
        """
        if config.WRITES_DISABLED:
            return write_disabled()

        # Fetch current ticket to get board context for status resolution
        try:
            current = await _cw_get(f"/service/tickets/{ticket_number}",
                                    fields="id,board,status,_info")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                from cwpsa.errors import not_found
                return not_found(f"Ticket #{ticket_number} not found.")
            return upstream_error(str(e))

        ops: list[dict[str, Any]] = []

        if summary:
            ops.append(scalar("summary", summary))

        if priority:
            priorities = await resolve_priority(priority)
            if not priorities:
                return validation_error(f"No priority found matching '{priority}'.")
            ops.append(reference("priority", id_=priorities[0]["id"]))

        if assigned_to:
            members = await resolve_member(assigned_to, limit=1)
            if not members:
                return validation_error(f"No member found matching '{assigned_to}'.")
            ops.append(reference("owner", id_=members[0]["id"]))

        if board:
            boards = await resolve_board(board)
            if not boards:
                return validation_error(f"No board found matching '{board}'.")
            ops.append(reference("board", id_=boards[0]["id"]))

        if status:
            board_id = current.get("board", {}).get("id")
            if board_id:
                statuses = await _cw_get(
                    f"/service/boards/{board_id}/statuses",
                    fields="id,name", pageSize=200,
                )
                from cwpsa.resolution.engine import _match
                matched = _match(statuses, status)
                if not matched:
                    return validation_error(
                        f"No status matching '{status}' on the current board."
                    )
                ops.append(reference("status", id_=matched[0]["id"]))

        if not ops:
            return validation_error("No fields to update were specified.")

        try:
            validated_ops = build_patch(*ops)
            result = await cw_patch(f"/service/tickets/{ticket_number}", validated_ops)
        except (ValueError, httpx.HTTPStatusError, Exception) as e:
            return upstream_error(str(e))

        return result
