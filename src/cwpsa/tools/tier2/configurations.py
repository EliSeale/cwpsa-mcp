"""
Tier 2 workflow tools — configurations/assets (§4.2).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error
from cwpsa.integration.client import cw_get as _cw_get
from cwpsa.resolution.engine import resolve_company


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def cw_list_configurations(
        company: str,
        type_filter: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any] | ErrorEnvelope:
        """List configurations (devices/assets) for a company.

        Args:
            company:     Company name or acronym (resolved automatically).
            type_filter: Configuration type name filter, e.g. "Server", "Workstation".
            page:        Page number.
            page_size:   Records per page.
        """
        matches = await resolve_company(company, limit=1)
        if not matches:
            return validation_error(f"No company found matching '{company}'.")

        company_id = matches[0]["id"]
        conditions = [f"company/id={company_id}", "activeFlag=True"]

        if type_filter:
            type_safe = type_filter.replace('"', '\\"')
            conditions.append(f'type/name contains "{type_safe}"')

        try:
            data = await _cw_get(
                "/company/configurations",
                conditions=" and ".join(f"({c})" for c in conditions),
                fields="id,name,company,type,contact,status,manufacturer,"
                       "model,serialNumber,installationDate,_info",
                pageSize=page_size,
                page=page,
                orderBy="name asc",
            )
        except Exception as e:
            return upstream_error(str(e))

        if not isinstance(data, list):
            data = [data] if data else []

        return {
            "company": matches[0],
            "entity": "company/configurations",
            "count_hint": len(data),
            "data": data,
            "has_more": len(data) >= page_size,
        }
