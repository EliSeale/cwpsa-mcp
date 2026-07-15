"""
Registry builder — distills the 11 MB ConnectWise OpenAPI spec into a compact
registry.json artifact (§5 of the architecture spec).

Run at CI time (not at server startup):
  python scripts/build_registry.py [--spec connectwise_openapi_complete.json] [--out registry.json]

Output: registry.json — loaded by cwpsa.registry.loader.get_registry() at startup.

Build pipeline:
  1. Parse the full OpenAPI spec (JSON).
  2. Identify collection endpoints (no path params, not count/info/search variants).
  3. For each entity, resolve the GET response schema → field properties.
  4. Classify each field (scalar/enum/ref/text/datetime/boolean/array).
  5. Score and compute default_projection (§14 algorithm).
  6. Determine allowed CRUD operations from sibling paths.
  7. Serialize compact registry.json.

ConnectWise rule: all fields returned by GET are filterable (conditions can only
reference fields present in the GET response).  Arrays and objects are not filterable.
Reference fields (schema ends in "Reference") are filterable via path traversal.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# §14 — Default projection scoring weights
# ---------------------------------------------------------------------------
_W_ID        = 100.0
_W_NAME      = 4.0    # name, identifier, summary, title, caption, firstName, lastName
_W_STATUS    = 3.0    # status, state, stage (scalar)
_W_REF_A     = 3.0    # company, contact, status (ref)
_W_REF_B     = 2.5    # board, priority, type, owner, member, agreement (ref)
_W_REF_C     = 2.0    # department, location, site, manufacturer, category (ref)
_W_REQUIRED  = 1.5
_W_KEY_FLAG  = 1.5    # closedFlag, inactiveFlag, deletedFlag, billableFlag, activeFlag
_W_TIMESTAMP = 1.0    # lastUpdated, dateEntered, dueDate, *Date
_W_SCALAR    = 1.0
_W_FREE_TEXT = -3.0   # description, notes, body, analysis, resolution, *Internal*
_W_ARRAY     = -3.0
_W_OBJECT    = -3.0
_W_AUDIT     = -3.0   # _info, enteredBy, updatedBy, guid, mobileGuid, *Identifier
_W_BOOL      = -1.5   # non-key boolean flags

_NAME_FIELDS = frozenset(["name", "identifier", "summary", "title", "caption",
                           "firstname", "lastname", "subject"])
_STATUS_FIELDS = frozenset(["status", "state", "stage", "phase"])
_KEY_FLAGS = frozenset(["closedflag", "inactiveflag", "deletedflag",
                        "disabledflag", "billableflag", "activeflag"])
_REF_A = frozenset(["company", "contact", "status"])
_REF_B = frozenset(["board", "priority", "type", "owner", "member", "agreement"])
_REF_C = frozenset(["department", "location", "site", "manufacturer",
                    "category", "subcategory", "team"])
_AUDIT_FIELDS = frozenset(["_info", "enteredby", "updatedby", "mobileguid", "guid"])
_FREE_TEXT_SUBSTR = ("description", "notes", "body", "analysis", "resolution",
                     "message", "comment", "internal", "initialresolution",
                     "initialdescription")

# Fields that are filterable by traversal (ref/name) but the BASE field is the ref obj
_TIMESTAMP_SUFFIX = ("date", "dateentered", "lastupdated", "duedate", "dateacquired",
                     "billstartdate", "enddate", "nextinvoicedate", "closeddate",
                     "dateresolved", "dateresponded", "timestart", "timeend")

# Sensitivity map (§5.3) — for RAG ACL in later phase
_INTERNAL_FIELDS = frozenset([
    "initialInternalAnalysis", "internalNotes", "addToInternalAnalysisFlag",
])
_CUSTOMER_FACING_FIELDS = frozenset([
    "initialDescription", "initialResolution",
    "addToDetailDescriptionFlag", "addToResolutionFlag",
])


# ---------------------------------------------------------------------------
# Field scoring
# ---------------------------------------------------------------------------

def _score(name: str, field_type: str, is_ref: bool, is_required: bool) -> float:
    lower = name.lower()

    if lower == "id":
        return _W_ID

    score = 0.0

    if lower in _NAME_FIELDS:
        score += _W_NAME
    if lower in _STATUS_FIELDS and not is_ref:
        score += _W_STATUS
    if lower in _KEY_FLAGS:
        score += _W_KEY_FLAG
    if is_required:
        score += _W_REQUIRED

    if is_ref:
        if lower in _REF_A:
            score += _W_REF_A
        elif lower in _REF_B:
            score += _W_REF_B
        elif lower in _REF_C:
            score += _W_REF_C
        else:
            score += _W_SCALAR
    elif field_type == "array":
        score += _W_ARRAY
    elif field_type in ("object", "custom_array"):
        score += _W_OBJECT if field_type == "object" else _W_ARRAY
    elif field_type == "boolean":
        if lower not in _KEY_FLAGS:
            score += _W_BOOL
        else:
            score += _W_KEY_FLAG
    elif field_type == "datetime":
        score += _W_TIMESTAMP
    elif field_type == "text":
        score += _W_FREE_TEXT
    elif field_type in ("string", "integer", "float", "enum"):
        score += _W_SCALAR

    if lower in _AUDIT_FIELDS or lower.endswith("identifier"):
        score += _W_AUDIT

    return score


# ---------------------------------------------------------------------------
# Field classification
# ---------------------------------------------------------------------------

def _classify_field(
    name: str,
    fschema: dict[str, Any],
    required_set: set[str],
    schemas: dict[str, Any],
    schema_to_entity: dict[str, str],
) -> dict[str, Any] | None:
    """Classify a schema field into a registry FieldMeta dict.

    Returns None for fields that should be excluded entirely (_info, arrays of
    complex objects that aren't customFields).
    """
    lower = name.lower()

    # Always exclude _info (it's a system envelope, not a filter field)
    if lower == "_info":
        return None

    # Audit/system fields — include but mark low-rank
    is_audit = lower in _AUDIT_FIELDS or lower.endswith("identifier")

    # customFields array — special handling
    if lower == "customfields":
        return {
            "type": "custom_array",
            "filterable": False,
            "sortable": False,
            "required": False,
            "filter_form": "customFieldConditions",
            "rank": _W_ARRAY,
            "visibility": "neutral",
        }

    ref_str = fschema.get("$ref", "")
    is_array = fschema.get("type") == "array"
    is_required = name in required_set

    # Array of items
    if is_array:
        items = fschema.get("items", {})
        items_ref = items.get("$ref", "")
        # Arrays are not filterable, but we record them
        if items_ref:
            items_schema_name = items_ref.split("/")[-1]
            # customFields array is already handled above
            return {
                "type": "array",
                "filterable": False,
                "sortable": False,
                "required": is_required,
                "rank": _W_ARRAY,
                "visibility": "neutral",
            }
        return {
            "type": "array",
            "filterable": False,
            "sortable": False,
            "required": is_required,
            "rank": _W_ARRAY,
            "visibility": "neutral",
        }

    # Reference field — $ref to a *Reference schema
    if ref_str:
        ref_schema_name = ref_str.split("/")[-1]
        is_ref = ref_schema_name.endswith("Reference")
        if is_ref:
            # Map Reference schema name → entity path
            base = ref_schema_name.replace("Reference", "")
            ref_entity = schema_to_entity.get(base)
            field_type = "ref"
            rank = _score(name, field_type, is_ref=True, is_required=is_required)
            if is_audit:
                rank += _W_AUDIT
            visibility = _get_visibility(name)
            result: dict[str, Any] = {
                "type": field_type,
                "filterable": True,   # refs are filterable via /name, /id traversal
                "sortable": False,    # refs themselves aren't sortable (use /name)
                "required": is_required,
                "rank": rank,
                "visibility": visibility,
            }
            if ref_entity:
                result["ref_entity"] = ref_entity
            # Check if status is scoped by board
            if lower == "status":
                result["scoped_by"] = "board"
            return result
        else:
            # Non-reference object ref (e.g. embedded objects)
            rank = _score(name, "object", is_ref=False, is_required=is_required)
            return {
                "type": "object",
                "filterable": False,
                "sortable": False,
                "required": is_required,
                "rank": rank,
                "visibility": "neutral",
            }

    # Scalar/primitive fields
    field_type_raw = fschema.get("type", "string")
    fmt = fschema.get("format", "")
    enums = fschema.get("enum")

    if field_type_raw == "object":
        return {
            "type": "object",
            "filterable": False,
            "sortable": False,
            "required": is_required,
            "rank": _W_OBJECT,
            "visibility": "neutral",
        }

    if field_type_raw == "boolean":
        rank = _score(name, "boolean", is_ref=False, is_required=is_required)
        return {
            "type": "boolean",
            "filterable": True,
            "sortable": False,
            "required": is_required,
            "rank": rank,
            "visibility": _get_visibility(name),
        }

    if field_type_raw in ("integer", "number"):
        py_type = "integer" if field_type_raw == "integer" else "float"
        rank = _score(name, py_type, is_ref=False, is_required=is_required)
        if is_audit:
            rank += _W_AUDIT
        return {
            "type": py_type,
            "filterable": True,
            "sortable": True,
            "required": is_required,
            "rank": rank,
            "visibility": "neutral",
        }

    # String fields
    if enums:
        rank = _score(name, "enum", is_ref=False, is_required=is_required)
        return {
            "type": "enum",
            "filterable": True,
            "sortable": True,
            "required": is_required,
            "values": [v for v in enums if v],
            "rank": rank,
            "visibility": _get_visibility(name),
        }

    if fmt == "date-time":
        rank = _score(name, "datetime", is_ref=False, is_required=is_required)
        return {
            "type": "datetime",
            "filterable": True,
            "sortable": True,
            "required": is_required,
            "rank": rank,
            "visibility": "neutral",
        }

    # Plain string — check for free text
    if _is_free_text(name):
        visibility = _get_visibility(name)
        return {
            "type": "text",
            "filterable": False,
            "sortable": False,
            "required": is_required,
            "rank": _W_FREE_TEXT,
            "visibility": visibility,
        }

    # Regular string scalar
    rank = _score(name, "string", is_ref=False, is_required=is_required)
    if is_audit:
        rank += _W_AUDIT
    return {
        "type": "string",
        "filterable": True,
        "sortable": True,
        "required": is_required,
        "rank": rank,
        "visibility": _get_visibility(name),
    }


def _is_free_text(name: str) -> bool:
    lower = name.lower()
    return any(p in lower for p in _FREE_TEXT_SUBSTR)


def _get_visibility(name: str) -> str:
    if name in _INTERNAL_FIELDS:
        return "internal_only"
    if name in _CUSTOMER_FACING_FIELDS:
        return "customer_facing"
    return "neutral"


# ---------------------------------------------------------------------------
# Response schema extraction
# ---------------------------------------------------------------------------

def _get_item_schema_name(path_item: dict[str, Any]) -> str | None:
    """Extract the schema name from a collection GET operation's 200 response."""
    get_op = path_item.get("get", {})
    resp = get_op.get("responses", {}).get("200", {})
    for ct_val in resp.get("content", {}).values():
        s = ct_val.get("schema", {})
        if s.get("type") == "array":
            ref = s.get("items", {}).get("$ref", "")
            if ref:
                return ref.split("/")[-1]
        ref = s.get("$ref", "")
        if ref:
            return ref.split("/")[-1]
    return None


# ---------------------------------------------------------------------------
# Operations inference from sibling paths
# ---------------------------------------------------------------------------

def _get_operations(
    entity_path: str, paths: dict[str, Any]
) -> list[str]:
    """Infer CRUD operations from the path items present in the spec."""
    ops = []
    collection_path = f"/{entity_path}"
    by_id_path = f"/{entity_path}/{{id}}"

    collection_item = paths.get(collection_path, {})
    by_id_item = paths.get(by_id_path, {})

    if "get" in collection_item:
        ops.append("query")
        ops.append("count")   # /count sibling is always present when GET exists
    if "get" in by_id_item:
        ops.append("get")
    if "post" in collection_item:
        ops.append("create")
    if "patch" in by_id_item or "put" in by_id_item:
        ops.append("update")
    if "delete" in by_id_item:
        ops.append("delete")

    return ops


# ---------------------------------------------------------------------------
# Natural identifier inference
# ---------------------------------------------------------------------------

def _natural_identifier(schema_props: dict[str, Any]) -> str | None:
    """Pick the best human-readable identifier field for this entity."""
    candidates = ["summary", "name", "identifier", "title", "caption", "subject",
                  "firstName", "lastName"]
    for c in candidates:
        if c in schema_props:
            return c
    return None


# ---------------------------------------------------------------------------
# Default projection computation
# ---------------------------------------------------------------------------

def _default_projection(
    fields: dict[str, dict[str, Any]], max_fields: int = 12
) -> list[str]:
    """Pick the top-ranked fields for the default projection (§14)."""
    # Force-include id
    ranked = sorted(
        [(name, meta["rank"]) for name, meta in fields.items()],
        key=lambda x: x[1],
        reverse=True,
    )
    result = []
    for name, rank in ranked:
        if rank <= 0 and name != "id":
            break
        if len(result) >= max_fields:
            break
        result.append(name)
    # Ensure id is always first
    if "id" in result:
        result.remove("id")
    return ["id"] + result[:max_fields - 1]


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_registry(spec: dict[str, Any]) -> dict[str, Any]:
    """Distill the OpenAPI spec into a compact registry dict."""
    schemas = spec.get("components", {}).get("schemas", {})
    paths = spec.get("paths", {})

    # Build schema_name → entity_path map for ref_entity resolution
    # e.g. "Company" → "company/companies", "Ticket" → "service/tickets"
    schema_to_entity: dict[str, str] = {}
    for path, path_item in paths.items():
        parts = path.strip("/").split("/")
        if any(p.startswith("{") for p in parts):
            continue
        if any(seg in ("count", "default", "info", "search", "schema") for seg in parts):
            continue
        sname = _get_item_schema_name(path_item)
        if sname and sname not in schema_to_entity:
            schema_to_entity[sname] = path.strip("/")

    log.debug("schema_to_entity: %d entries", len(schema_to_entity))

    entities: dict[str, Any] = {}

    for path, path_item in paths.items():
        parts = path.strip("/").split("/")

        # Only process collection endpoints (no path params, no utility suffixes)
        if any(p.startswith("{") for p in parts):
            continue
        if any(seg in ("count", "default", "info", "search", "schema") for seg in parts):
            continue
        if "get" not in path_item:
            continue

        entity_key = path.strip("/")
        schema_name = _get_item_schema_name(path_item)
        if not schema_name:
            continue

        entity_schema = schemas.get(schema_name)
        if not entity_schema:
            # Some schemas are referenced but not defined (e.g. primitive arrays)
            log.debug("Schema '%s' not found for entity '%s' — skipping", schema_name, entity_key)
            continue

        required_set = set(entity_schema.get("required", []))
        props = entity_schema.get("properties", {})

        # Classify all fields
        fields: dict[str, Any] = {}
        for fname, fschema in props.items():
            meta = _classify_field(
                fname, fschema, required_set, schemas, schema_to_entity
            )
            if meta is not None:
                fields[fname] = meta

        # Determine operations
        ops = _get_operations(entity_key, paths)

        # Default projection
        projection = _default_projection(fields)

        # Natural identifier
        nat_id = _natural_identifier(props)

        # Detect reporting endpoints (path contains /reports or uses "columns")
        get_op = path_item.get("get", {})
        param_names = [p.get("name") for p in get_op.get("parameters", [])]
        uses_columns = "columns" in param_names and "fields" not in param_names

        entities[entity_key] = {
            "entity": entity_key,
            "id_field": "id",
            "natural_identifier": nat_id,
            "schema_name": schema_name,
            "operations": ops,
            "default_projection": projection,
            "fields": fields,
            "custom_fields": [],          # merged at runtime from system/userDefinedFields
            "uses_columns_param": uses_columns,
            "supports_search_post": True,  # all collection endpoints support /search POST
        }

    log.info("Extracted %d entities from spec", len(entities))

    return {
        "version": "0.2.0",
        "spec_version": spec.get("info", {}).get("version", ""),
        "entities": entities,
        "alias_map": {},   # seed alias map is merged by registry/loader.py at startup
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the cwpsa registry artifact from a ConnectWise OpenAPI spec."
    )
    parser.add_argument(
        "--spec", default="connectwise_openapi_complete.json",
        help="Path to the ConnectWise OpenAPI JSON spec (default: connectwise_openapi_complete.json).",
    )
    parser.add_argument(
        "--out", default="registry.json",
        help="Output path for the registry artifact (default: registry.json).",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    spec_path = Path(args.spec)
    if not spec_path.exists():
        log.error("Spec not found: %s", spec_path)
        sys.exit(1)

    size_mb = spec_path.stat().st_size / 1_000_000
    log.info("Loading spec: %s (%.1f MB)...", spec_path, size_mb)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    log.info("Building registry...")
    registry = build_registry(spec)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    out_size_kb = out_path.stat().st_size / 1000
    log.info(
        "Registry written: %s — %d entities, %.1f KB (spec was %.1f MB).",
        out_path,
        len(registry["entities"]),
        out_size_kb,
        size_mb,
    )

    # Quick sanity: print a few entity summaries
    for ename in list(registry["entities"])[:5]:
        e = registry["entities"][ename]
        log.info(
            "  %-45s  %3d fields  ops: %s",
            ename,
            len(e["fields"]),
            ",".join(e["operations"]),
        )


if __name__ == "__main__":
    main()
