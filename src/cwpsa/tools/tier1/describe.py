"""
cw_describe — Tier 1 tool: field manifest for any ConnectWise entity (§4.1).

Returns the entity's fields, types, filterability, static enums, reference
markers, and default projection from the registry.  Custom fields (tenant data)
are merged in from the reference cache.

Schema-on-demand: the agent fetches the manifest for the one entity it is
working with rather than pre-loading all 283 schemas.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa.registry.loader import get_registry


def register(mcp: FastMCP) -> None:
    """Register cw_describe on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
        }
    )
    async def cw_describe(
        entity: str,
        full: bool = False,
    ) -> dict[str, Any]:
        """Return the field manifest for a ConnectWise entity.

        Use this before calling cw_query or cw_get to understand what fields
        are available, which are filterable/sortable, and what enum values are allowed.

        Args:
            entity: The entity path, e.g. "service/tickets", "company/companies",
                    "time/entries", "finance/agreements".
                    Call cw_describe with entity="?" to list all available entities.
            full:   False (default) returns the lean default projection fields only.
                    True returns all fields including low-rank ones.

        Returns a dict with:
            entity:             the canonical entity path
            operations:         allowed CRUD operations
            default_projection: recommended fields for list views
            fields:             {field_name: {type, filterable, sortable, values?, ref_entity?}}
            custom_fields:      tenant-defined custom fields (merged from live API)
        """
        registry = get_registry()

        # List all entities
        if entity in ("?", "", "list"):
            return {
                "entities": registry.entity_names(),
                "hint": "Pass one of these entity paths to cw_describe to see its fields.",
            }

        # Try alias resolution
        resolved = registry.resolve_alias(entity)
        if resolved and resolved != entity:
            entity = resolved

        record = registry.get_entity(entity)
        if record is None:
            # Suggest close matches
            import difflib
            close = difflib.get_close_matches(entity, registry.entity_names(), n=5, cutoff=0.5)
            return {
                "error": f"Entity '{entity}' not found in registry.",
                "did_you_mean": close,
            }

        # Build response
        if full:
            fields_to_return = dict(record.fields)
        else:
            # Lean default: only fields in default_projection + high-rank fields
            proj_set = set(record.default_projection)
            fields_to_return = {
                name: meta
                for name, meta in record.fields.items()
                if name in proj_set or meta.rank >= 3.0
            }

        return {
            "entity": record.entity,
            "operations": record.operations,
            "default_projection": record.default_projection,
            "fields": {
                name: {
                    "type": meta.type,
                    "filterable": meta.filterable,
                    "sortable": meta.sortable,
                    "required": meta.required,
                    **({"values": meta.values} if meta.values else {}),
                    **({"ref_entity": meta.ref_entity} if meta.ref_entity else {}),
                    **({"scoped_by": meta.scoped_by} if meta.scoped_by else {}),
                    "rank": meta.rank,
                }
                for name, meta in fields_to_return.items()
            },
            "custom_fields": [
                {
                    "id": cf.id,
                    "caption": cf.caption,
                    "type": cf.type,
                }
                for cf in record.custom_fields
            ],
            "note": (
                "full=true to see all fields"
                if not full and len(fields_to_return) < len(record.fields)
                else None
            ),
        }
