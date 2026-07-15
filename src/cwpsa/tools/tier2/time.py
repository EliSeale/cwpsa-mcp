"""
Tier 2 workflow tools — time entries (§4.2).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa import config
from cwpsa.auth.pep import write_auth_check
from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error, write_disabled
from cwpsa.integration.client import cw_post
from cwpsa.resolution.engine import resolve_company, resolve_member

import httpx


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        auth=write_auth_check,
    )
    async def cw_log_time(
        ticket_number: int,
        hours: float,
        notes: str,
        member: str | None = None,
        billable_option: str = "Billable",
        time_start: str | None = None,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Log time against a service ticket.

        Args:
            ticket_number:   The numeric ticket ID.
            hours:           Actual hours worked (e.g. 1.5 for 1h30m).
            notes:           Work notes — added to the ticket time entry.
                             Customer-facing notes appear on invoices.
            member:          Member name or login.  Defaults to the authenticated member.
            billable_option: One of: Billable, DoNotBill, NoCharge, NoDefault.
                             Default: Billable.
            time_start:      When the work started (ISO-8601 UTC, e.g. "2026-07-06T09:00:00Z").
                             Defaults to now if omitted.

        Returns the created time entry record.
        """
        if config.WRITES_DISABLED:
            return write_disabled()

        valid_billable = {"Billable", "DoNotBill", "NoCharge", "NoDefault"}
        if billable_option not in valid_billable:
            return validation_error(
                f"Invalid billableOption '{billable_option}'.",
                allowed_values=sorted(valid_billable),
            )

        body: dict[str, Any] = {
            "chargeToId": ticket_number,
            "chargeToType": "ServiceTicket",
            "actualHours": hours,
            "notes": notes,
            "billableOption": billable_option,
        }

        if member:
            members = await resolve_member(member, limit=1)
            if not members:
                return validation_error(f"No member found matching '{member}'.")
            body["member"] = {"id": members[0]["id"]}

        if time_start:
            body["timeStart"] = time_start

        try:
            result = await cw_post("/time/entries", body)
        except httpx.HTTPStatusError as e:
            return upstream_error(
                f"ConnectWise {e.response.status_code}: {e.response.text[:300]}"
            )
        except Exception as e:
            return upstream_error(str(e))

        return result
