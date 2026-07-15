"""
cw_query — Tier 1 tool: filtered list of any ConnectWise entity (§4.1, §4.4, §6).

Accepts a structured FilterDSL; the server compiles to ConnectWise conditions.
The agent NEVER writes raw conditions strings.
Response-size governance (§4.4): capped page, has_more + cursor for continuation.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa import config
from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error
from cwpsa.filter import FilterDSL
from cwpsa.filter.compiler import compile_filter
from cwpsa.filter.validator import validate_filter
from cwpsa.integration.client import cw_get, cw_search_post
from cwpsa.registry.loader import get_registry


def register(mcp: FastMCP) -> None:
    """Register cw_query on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
        }
    )
    async def cw_query(
        entity: str,
        filter: dict[str, Any] | None = None,
        response_format: str = "concise",
    ) -> dict[str, Any] | ErrorEnvelope:
        """Return a filtered, paginated list of any ConnectWise entity.

        The `filter` parameter accepts a structured FilterDSL object (see below).
        The server compiles it to ConnectWise's proprietary conditions syntax —
        you never write conditions strings directly.

        Args:
            entity:          Entity path, e.g. "service/tickets", "company/companies".
                             Call cw_describe(entity) first to see available fields.
            filter:          Structured filter object (FilterDSL).  All keys optional.
                             {
                               "conditions": <ConditionNode>,
                               "child_conditions": <ConditionNode>,
                               "custom_field_conditions": <ConditionNode>,
                               "order_by": [{"field": "...", "dir": "asc|desc"}],
                               "fields": ["id", "summary", ...],
                               "page": 1,
                               "page_size": 25,
                               "page_id": "<cursor>"   // for forward-only pagination
                             }
                             ConditionNode: leaf {"field","op","value"} or
                                            {"and":[...]} / {"or":[...]} group.
                             Operators: = != < <= > >= contains not_contains
                                        like not_like in not_in
                             Values: strings (auto-quoted), datetimes as UTC ISO-8601
                                     "2026-06-01T00:00:00Z", booleans, numbers, null.
            response_format: "concise" (default) — projected summary slice + has_more.
                             "detailed" — all fields returned by CW for each record.

        Returns:
            {
              "entity":      "<entity>",
              "count_hint":  <page count>,
              "data":        [...records...],
              "has_more":    true/false,
              "next_cursor": "<pageId or page number>",
              "message":     "showing N of M — refine filters or page for more"
            }
        """
        registry = get_registry()
        record = registry.get_entity(entity)
        if record is None:
            alias = registry.resolve_alias(entity)
            if alias:
                record = registry.get_entity(alias)
                entity = alias
        if record is None:
            import difflib
            close = difflib.get_close_matches(entity, registry.entity_names(), n=3, cutoff=0.5)
            return validation_error(
                f"Unknown entity '{entity}'.",
                suggestions=close,
            )

        # Parse and validate the filter DSL
        dsl = FilterDSL.model_validate(filter or {})

        if record and config.REGISTRY_PATH:  # registry exists
            err = validate_filter(dsl, record)
            if err:
                return err

        # Compile to query params
        try:
            compiled = compile_filter(dsl)
        except ValueError as e:
            return validation_error(str(e))

        # Choose default projection when no fields specified
        if not dsl.fields and record.default_projection:
            compiled.params["fields"] = ",".join(record.default_projection)
        elif dsl.fields:
            param_key = "columns" if record.uses_columns_param else "fields"
            compiled.params[param_key] = ",".join(dsl.fields)

        # Execute — use /search POST if flagged
        try:
            if compiled.use_search_post:
                conditions_str = str(compiled.params.pop("conditions", ""))
                data = await cw_search_post(f"/{entity}", conditions_str, **compiled.params)
            else:
                data = await cw_get(f"/{entity}", **compiled.params)
        except Exception as e:
            return upstream_error(f"ConnectWise error: {e}")

        if not isinstance(data, list):
            data = [data] if data else []

        # Response-size governance (§4.4) — indicate has_more via page size proxy
        page_size = dsl.page_size
        has_more = len(data) >= page_size
        next_cursor: int | str | None = None
        if has_more:
            next_cursor = (dsl.page or 1) + 1

        # _links digest from first record (§4.4/§4.9)
        links: list[dict] = []
        if data and isinstance(data[0], dict) and response_format == "concise":
            from cwpsa.links import extract_links
            links = extract_links(data[0], config.CW_BASE_URL, response_format)

        result: dict = {
            "entity": entity,
            "count_hint": len(data),
            "data": data,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "message": (
                f"showing {len(data)} records -- use next_cursor to page for more"
                if has_more
                else f"showing all {len(data)} records"
            ),
        }
        if links:
            result["_links"] = links
        return result
