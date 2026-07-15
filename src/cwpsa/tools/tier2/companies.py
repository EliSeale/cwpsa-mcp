"""
Tier 2 workflow tools — companies and contacts (§4.2).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error
from cwpsa.integration.client import cw_get as _cw_get
from cwpsa.resolution.engine import resolve_company


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def cw_find_company(
        name: str,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Find a company by name or acronym with fuzzy matching + disambiguation.

        Returns ranked candidates.  If exactly one match is found, also fetches
        the full company record and team members (for account manager lookup).

        Args:
            name: Company name, acronym, or partial name.
                  Examples: "ACME", "McDonald", "Verve"
        """
        matches = await resolve_company(name)
        if not matches:
            return validation_error(f"No company found matching '{name}'.")

        if len(matches) == 1:
            # Fetch full record + teams in one shot
            company_id = matches[0]["id"]
            try:
                full = await _cw_get(f"/company/companies/{company_id}")
                try:
                    teams = await _cw_get(
                        f"/company/companies/{company_id}/teams",
                        fields="id,member,role",
                        pageSize=50,
                    )
                except Exception:
                    teams = []
                full["_teams"] = teams
                return full
            except Exception as e:
                return upstream_error(str(e))

        # Multiple matches — return candidates for disambiguation
        return {
            "message": f"Multiple companies match '{name}'. Confirm with the user which one.",
            "candidates": matches,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def cw_list_contacts(
        company: str,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any] | ErrorEnvelope:
        """List contacts for a company.

        Args:
            company:   Company name or acronym (resolved automatically).
            page:      Page number.
            page_size: Records per page.
        """
        companies = await resolve_company(company, limit=1)
        if not companies:
            return validation_error(f"No company found matching '{company}'.")

        company_id = companies[0]["id"]
        try:
            data = await _cw_get(
                "/company/contacts",
                conditions=f"company/id={company_id} and inactiveFlag=False",
                fields="id,firstName,lastName,title,department,site,defaultContactFlag,communicationItems",
                pageSize=page_size,
                page=page,
                orderBy="lastName asc",
            )
        except Exception as e:
            return upstream_error(str(e))

        if not isinstance(data, list):
            data = [data] if data else []

        return {
            "company": companies[0],
            "count_hint": len(data),
            "data": data,
            "has_more": len(data) >= page_size,
        }
