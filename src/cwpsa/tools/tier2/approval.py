"""
Tier 2 action tool — approvals (approve / reject / reverse / submit).

cw_set_approval dispatches the approval verbs that exist on BOTH time sheets and
expense reports. The verbs are asymmetric: approve takes a body with an
approvalType (tiered), while reject, reverse, and submit take no body.

submit is a different actor: it is the record owner sending their OWN record for
approval, not a manager acting on someone else's. It shares this tool because it
is the same status-transition surface on the same records, but ConnectWise
enforces its permission as ownership, not approval authority.

Write tool: requires the mcp.write scope (auth=write_auth_check), honors the
CW_WRITES_DISABLED kill-switch, and runs under the caller's impersonated
ConnectWise member (so ConnectWise enforces whether the member may approve at
the requested tier). The name is registered in pep.py _WRITE_TOOLS.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastmcp import FastMCP

from cwpsa import config
from cwpsa.auth.pep import write_auth_check
from cwpsa.errors import (
    ErrorEnvelope,
    not_found,
    upstream_error,
    validation_error,
    write_disabled,
)
from cwpsa.integration.client import cw_get as _cw_get, cw_post
from cwpsa.tools.tier2._action import needs_input, preview

# approvalType enum (approve only), from the spec.
_APPROVAL_TYPES = (
    "DataEntry", "Tier1Update", "Tier2Update", "Billing", "Service",
    "Project", "MonthlySummary", "SalesActivity", "Schedule",
)

_ENTITY_BASE = {
    "time_sheet": "/time/sheets",
    "expense_report": "/expense/reports",
}

# Statuses where a further "approve" is a no-op (best-effort; tier config varies).
_FULLY_APPROVED = {"ApprovedByTierTwo", "ReadyToBill", "Billed", "BilledAgreement"}


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": False},
        auth=write_auth_check,
    )
    async def cw_set_approval(
        entity: Literal["time_sheet", "expense_report"],
        record_id: int,
        decision: Literal["approve", "reject", "reverse", "submit"],
        approval_type: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Approve, reject, reverse, or submit a time sheet or expense report.

        approve  -> needs approval_type (the tier), e.g. Tier1Update, Tier2Update.
        reject   -> sends the record back; no approval_type.
        reverse  -> undoes a prior approval; no approval_type.
        submit   -> the owner submits their own record for approval; no approval_type.

        Call with confirm=false first to see the record's current status and, for
        approve, the valid approval_type values. Then call with confirm=true to act.

        Args:
            entity:        "time_sheet" or "expense_report".
            record_id:     The sheet or report id.
            decision:      approve | reject | reverse | submit.
            approval_type: The tier to approve at (approve only); one of the
                           approvalType enum values.
            confirm:       Must be true to perform the action.
        """
        if config.WRITES_DISABLED:
            return write_disabled()

        base = _ENTITY_BASE[entity]

        # Read current status for context and a light pre-check.
        try:
            rec = await _cw_get(f"{base}/{record_id}", fields="id,status")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return not_found(f"{entity.replace('_', ' ')} #{record_id} not found.")
            return upstream_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            return upstream_error(str(exc))
        status = (rec or {}).get("status")

        # approve requires a valid approval_type; surface the enum if absent.
        if decision == "approve":
            if approval_type is None:
                return needs_input(
                    "set_approval",
                    missing=["approval_type"],
                    choices={"approval_type": list(_APPROVAL_TYPES), "current_status": status},
                    next_hint="call again with approval_type and confirm=true to approve",
                )
            if approval_type not in _APPROVAL_TYPES:
                return validation_error(
                    f"invalid approval_type '{approval_type}'.",
                    allowed_values=list(_APPROVAL_TYPES),
                )
            if status in _FULLY_APPROVED:
                return validation_error(
                    f"{entity.replace('_', ' ')} #{record_id} is already '{status}'; nothing to approve."
                )

        # Preview (nothing written).
        if not confirm:
            will = f"{decision} {entity.replace('_', ' ')} #{record_id} (current status: {status})"
            if decision == "approve":
                will += f" at tier '{approval_type}'"
            return preview("set_approval", will=will, entity=entity, record_id=record_id,
                           decision=decision, approval_type=approval_type, current_status=status)

        # Execute. approve carries a body; reject/reverse/submit are bodyless.
        path = f"{base}/{record_id}/{decision}"
        body: dict[str, Any] = (
            {"id": record_id, "approvalType": approval_type} if decision == "approve" else {}
        )
        try:
            result = await cw_post(path, body)
        except httpx.HTTPStatusError as exc:
            return upstream_error(
                f"{decision} failed: {exc.response.status_code} {exc.response.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            return upstream_error(f"{decision} failed: {exc}")

        return {"status": "completed", "entity": entity, "record_id": record_id,
                "decision": decision, "result": result if result else "ok"}
