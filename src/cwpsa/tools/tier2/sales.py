"""
Tier 2 workflow tools — sales/opportunities (§4.2).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error
from cwpsa.integration.client import cw_get as _cw_get
from cwpsa.resolution.engine import resolve_company


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def cw_list_opportunities(
        company: str | None = None,
        stage: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any] | ErrorEnvelope:
        """List sales opportunities.

        Args:
            company:   Company name or acronym (resolved automatically).
            stage:     Opportunity stage name, e.g. "Proposal", "Closed Won".
            page:      Page number.
            page_size: Records per page.
        """
        conditions: list[str] = ["closedFlag=False"]

        if company:
            matches = await resolve_company(company, limit=1)
            if not matches:
                return validation_error(f"No company found matching '{company}'.")
            conditions.append(f"company/id={matches[0]['id']}")

        if stage:
            stage_safe = stage.replace('"', '\\"')
            conditions.append(f'stage/name contains "{stage_safe}"')

        try:
            data = await _cw_get(
                "/sales/opportunities",
                conditions=" and ".join(f"({c})" for c in conditions),
                fields="id,name,company,contact,stage,status,closeDate,expectedCloseDate,"
                       "probability,forecastAmount,assignedTo,_info",
                pageSize=page_size,
                page=page,
                orderBy="expectedCloseDate asc",
            )
        except Exception as e:
            return upstream_error(str(e))

        if not isinstance(data, list):
            data = [data] if data else []

        return {
            "entity": "sales/opportunities",
            "count_hint": len(data),
            "data": data,
            "has_more": len(data) >= page_size,
        }
