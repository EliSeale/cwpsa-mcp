"""
Tier 2 workflow tools — finance: invoices and agreements (§4.2).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error
from cwpsa.integration.client import cw_get as _cw_get
from cwpsa.resolution.engine import resolve_company


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def cw_list_invoices(
        company: str | None = None,
        status: str | None = None,
        date_range: dict[str, str] | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any] | ErrorEnvelope:
        """List invoices with optional company/status/date filters.

        Args:
            company:    Company name or acronym (resolved automatically).
            status:     Invoice status name, e.g. "Draft", "Sent", "Paid".
            date_range: {"from": "2026-01-01T00:00:00Z", "to": "2026-06-30T23:59:59Z"}
            page:       Page number.
            page_size:  Records per page.
        """
        conditions: list[str] = []

        if company:
            matches = await resolve_company(company, limit=1)
            if not matches:
                return validation_error(f"No company found matching '{company}'.")
            conditions.append(f"company/id={matches[0]['id']}")

        if status:
            status_safe = status.replace('"', '\\"')
            conditions.append(f'status/name="{status_safe}"')

        if date_range:
            from_ = date_range.get("from")
            to_ = date_range.get("to")
            if from_:
                conditions.append(f"date>=[{from_}]")
            if to_:
                conditions.append(f"date<=[{to_}]")

        params: dict[str, Any] = {
            "fields": "id,company,status,type,date,dueDate,total,balance,department,_info",
            "pageSize": page_size,
            "page": page,
            "orderBy": "date desc",
        }
        if conditions:
            params["conditions"] = " and ".join(f"({c})" for c in conditions)

        try:
            data = await _cw_get("/finance/invoices", **params)
        except Exception as e:
            return upstream_error(str(e))

        if not isinstance(data, list):
            data = [data] if data else []

        return {
            "entity": "finance/invoices",
            "count_hint": len(data),
            "data": data,
            "has_more": len(data) >= page_size,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def cw_get_agreement(
        company: str,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Get active agreements for a company.

        Args:
            company: Company name or acronym (resolved automatically).
        """
        matches = await resolve_company(company, limit=1)
        if not matches:
            return validation_error(f"No company found matching '{company}'.")

        company_id = matches[0]["id"]
        try:
            data = await _cw_get(
                "/finance/agreements",
                conditions=f"company/id={company_id} and cancelledFlag=False",
                fields="id,name,company,type,agreementStatus,billStartDate,endDate,"
                       "nextInvoiceDate,billAmount,agreementStatus,_info",
                pageSize=50,
                orderBy="endDate asc",
            )
        except Exception as e:
            return upstream_error(str(e))

        if not isinstance(data, list):
            data = [data] if data else []

        return {
            "company": matches[0],
            "agreements": data,
            "count": len(data),
        }
