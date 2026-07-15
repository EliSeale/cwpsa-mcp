"""
cw_count — Tier 1 tool: count matching records cheaply before a large query (§4.1).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error
from cwpsa.filter import FilterDSL
from cwpsa.filter.compiler import compile_filter
from cwpsa.filter.validator import validate_filter
from cwpsa.integration.client import cw_count as _cw_count
from cwpsa.registry.loader import get_registry


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
        }
    )
    async def cw_count(
        entity: str,
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Return the count of records matching a filter for any ConnectWise entity.

        Cheap pre-check before deciding whether to paginate a large cw_query.
        Accepts the same `filter` DSL as cw_query (conditions/child_conditions/
        custom_field_conditions only — order_by, fields, and pagination are ignored).

        Args:
            entity: Entity path, e.g. "service/tickets".
            filter: Optional filter DSL (same shape as cw_query's filter parameter).

        Returns:
            {"entity": "...", "count": <int>}
        """
        registry = get_registry()
        record = registry.get_entity(entity)
        if record is None:
            import difflib
            close = difflib.get_close_matches(entity, registry.entity_names(), n=3, cutoff=0.5)
            return validation_error(f"Unknown entity '{entity}'.", suggestions=close)

        # Parse filter (only conditions parts matter for count)
        dsl_data = {k: v for k, v in (filter or {}).items()
                    if k in ("conditions", "child_conditions", "custom_field_conditions")}
        try:
            dsl = FilterDSL.model_validate(dsl_data)
        except Exception as e:
            return validation_error(str(e))

        # Pre-flight validation against registry (before hitting the API)
        if record.fields:
            err = validate_filter(dsl, record)
            if err:
                return err

        try:
            compiled = compile_filter(dsl)
        except ValueError as e:
            return validation_error(str(e))

        # Extract only the conditions params
        count_params = {
            k: v for k, v in compiled.params.items()
            if k in ("conditions", "childConditions", "customFieldConditions")
        }

        try:
            count = await _cw_count(f"/{entity}", **count_params)
        except Exception as e:
            return upstream_error(str(e))

        return {"entity": entity, "count": count}
