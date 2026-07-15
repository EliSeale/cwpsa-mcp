"""
cw_get — Tier 1 tool: single authoritative record by ID (§4.1).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.errors import ErrorEnvelope, not_found, upstream_error
from cwpsa.integration.client import cw_get as _cw_get
from cwpsa.registry.loader import get_registry

import httpx


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
        }
    )
    async def cw_get(
        entity: str,
        id: int,
        fields: list[str] | None = None,
        response_format: str = "concise",
    ) -> dict[str, Any] | ErrorEnvelope:
        """Retrieve a single ConnectWise record by its numeric ID.

        Args:
            entity:          Entity path, e.g. "service/tickets", "company/companies".
            id:              Numeric record ID.
            fields:          Optional list of field paths to return.  If omitted, the
                             entity's default projection is used.  Pass ["*"] for all fields.
            response_format: "concise" (default) strips raw _info and returns a `_links`
                             digest of navigable relations. "detailed" returns full _info.

        Returns the record dict with:
          _version:  lastUpdated timestamp for optimistic concurrency (cw_update).
          _links:    Navigable ConnectWise API relations from _info (graph navigation,
                     concise mode only). Pass a `_links[].rel` to cw_follow_href.
        """
        registry = get_registry()
        record = registry.get_entity(entity)

        params: dict[str, Any] = {}
        if fields and fields != ["*"]:
            params["fields"] = ",".join(fields)
        elif record and record.default_projection and not fields:
            # Always include _info for the version token
            projection = record.default_projection + ["_info"]
            params["fields"] = ",".join(projection)

        try:
            data = await _cw_get(f"/{entity}/{id}", **params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return not_found(f"{entity} with id={id} not found.")
            return upstream_error(f"ConnectWise error {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            return upstream_error(str(e))

        # Expose _info.lastUpdated as a top-level _version for optimistic concurrency
        if isinstance(data, dict) and "_info" in data:
            data["_version"] = data["_info"].get("lastUpdated")

        # Attach _links digest (graph navigation, §4.4/§4.9)
        if isinstance(data, dict):
            from cwpsa.links import attach_links
            from cwpsa import config as _cfg
            attach_links(data, _cfg.CW_BASE_URL, response_format)

        return data
