"""
Search entity/index registry + OData filter compiler (§2, §3, §7 of the RAG plan).

A single generic ``cw_search`` tool is entity-parameterized against this registry,
exactly mirroring how the Tier 1 CRUD tools are genericized over the entity
registry.  Adding a new searchable entity is one ``SearchEntity`` entry plus the
index itself — the tool code does not change (§7).

Each entry ties together, for one entity:
  * ``index``        — the Azure AI Search index name (the caller never passes this).
  * ``cw_entity``    — the ConnectWise entity path used for live hydration.
  * ``id_field``     — the index field holding the ConnectWise record id (the key we
                       hydrate by).  ConnectWise ids are numeric; the index may store
                       them as strings.
  * ``label_field``  — the human-readable field surfaced in evidence (e.g. ``summary``).
  * ``content_field``/``vector_field`` — the free-text and vector fields (the vector
                       field is non-retrievable; content supplies the evidence snippet).
  * ``semantic_config`` — the index's semantic configuration (L2 reranker).
  * ``filters``      — the structured filter fields this index supports, with the
                       type used to format each OData literal safely.

Filter compilation is server-side ONLY: the agent passes a structured object of
``{field: value}`` and never a raw filter string — the same conditions-injection
stance as the Tier 1 filter DSL (§3, §6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# Formatting type for an index filter field — drives OData literal rendering.
FilterType = Literal["string", "boolean", "number", "datetime", "collection"]


class SearchFilterError(ValueError):
    """Raised when a structured filter is invalid (unknown field, bad shape/value).

    The cw_search tool converts this into a ``validation_error`` envelope so the
    agent can correct the filter rather than the server emitting a raw filter
    string (injection-prevention stance, §3).
    """


@dataclass(frozen=True)
class FilterField:
    """One filterable index field: the agent-facing name maps to an index field."""

    index_field: str
    type: FilterType
    description: str = ""


@dataclass(frozen=True)
class SearchEntity:
    """Registry entry describing one searchable entity (§7)."""

    entity: str
    index: str
    cw_entity: str
    label_field: str
    id_field: str = "id"
    content_field: str = "content"
    vector_field: str = "contentVector"
    semantic_config: str | None = None
    filters: dict[str, FilterField] = field(default_factory=dict)

    def filter_field_names(self) -> list[str]:
        return sorted(self.filters.keys())

    def evidence_select(self) -> list[str]:
        """Index fields to retrieve for the evidence block (never the vector, §3/§4).

        Kept minimal: the id we hydrate by, the display label, and — where present
        — an index ``lastUpdated`` so the agent can gauge how stale the matched
        snippet was (§5).  The matched passage itself comes from @search.captions,
        not from selecting the full ``content`` field.
        """
        fields = {self.id_field, self.label_field}
        # A conventional index freshness field, if the schema carries one.
        for f in self.filters.values():
            if f.type == "datetime" and f.index_field in ("lastUpdated", "dateEntered"):
                fields.add(f.index_field)
        return sorted(fields)


# ---------------------------------------------------------------------------
# The registry — Phase A ships tickets (the existing cw-tickets index, §11).
# Projects / opportunities / companies are Phase B: add an entry here + the
# index; no tool change (§7, §11).
# ---------------------------------------------------------------------------
# Field names below follow the deployed cw-tickets schema described in the plan
# (§3: BM25 + `ticket-vector-profile` cosine 3072-dim + `ticket-semantic-config`;
# `content` retrievable, `contentVector` non-retrievable; every structured field
# filterable — §3 "Filtering"). If the live schema differs, this one entry is the
# only place to adjust.

SEARCH_REGISTRY: dict[str, SearchEntity] = {
    "tickets": SearchEntity(
        entity="tickets",
        index="cw-tickets",
        cw_entity="service/tickets",
        label_field="summary",
        id_field="id",
        content_field="content",
        vector_field="contentVector",
        semantic_config="ticket-semantic-config",
        filters={
            "company": FilterField("company", "string", "Company name as indexed."),
            "board": FilterField("board", "string", "Service board name."),
            "status": FilterField("status", "string", "Ticket status name."),
            "type": FilterField("type", "string", "Ticket type name."),
            "priority": FilterField("priority", "string", "Priority name."),
            "closedFlag": FilterField("closedFlag", "boolean", "Whether the ticket is closed."),
            "isChildTicket": FilterField("isChildTicket", "boolean", "Whether it is a child."),
            "recordType": FilterField("recordType", "string", "ConnectWise record type."),
            "dateEntered": FilterField("dateEntered", "datetime", "When the ticket was opened."),
            "lastUpdated": FilterField("lastUpdated", "datetime", "When the ticket last changed."),
        },
    ),
}


def get_search_entity(entity: str) -> SearchEntity | None:
    """Return the registry entry for a searchable entity, or None.

    Accepts the short entity name (``tickets``) or the ConnectWise entity path
    (``service/tickets``) so callers can pass whichever they already hold.
    """
    key = (entity or "").strip().lower()
    if key in SEARCH_REGISTRY:
        return SEARCH_REGISTRY[key]
    # Allow the CW entity path form.
    for se in SEARCH_REGISTRY.values():
        if se.cw_entity.lower() == key:
            return se
    return None


def searchable_entities() -> list[dict[str, Any]]:
    """Describe the search surface for discovery (extends cw_describe, §2)."""
    out: list[dict[str, Any]] = []
    for se in SEARCH_REGISTRY.values():
        out.append(
            {
                "entity": se.entity,
                "cw_entity": se.cw_entity,
                "index": se.index,
                "label_field": se.label_field,
                "filter_fields": {
                    name: {"type": ff.type, "description": ff.description}
                    for name, ff in sorted(se.filters.items())
                },
            }
        )
    return out


# ---------------------------------------------------------------------------
# OData $filter compilation (§3 "Filtering")
# ---------------------------------------------------------------------------
# Azure AI Search filters use OData syntax. Values are rendered by type so raw
# agent input is never string-concatenated into the filter expression:
#   * string   -> 'escaped'           (single quotes doubled)
#   * boolean  -> true / false
#   * number   -> bare
#   * datetime -> bare Edm.DateTimeOffset literal (validated ISO-8601 UTC)
#   * range    -> {"from": ..., "to": ...} -> ge / le
#   * list     -> OR of eq (or ge/le pair for datetime handled via range)

_DT_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _odata_string(value: Any) -> str:
    """Render an OData string literal, escaping embedded single quotes."""
    s = str(value).replace("'", "''")
    return f"'{s}'"


def _odata_datetime(value: Any) -> str:
    """Render (and validate) an OData Edm.DateTimeOffset literal — unquoted."""
    s = str(value)
    if not _DT_UTC_RE.match(s):
        raise SearchFilterError(
            f"Datetime value '{s}' must be UTC ISO-8601 with a trailing 'Z' "
            "(e.g. '2026-06-01T00:00:00Z')."
        )
    return s


def _render_scalar(ff: FilterField, value: Any) -> str:
    """Render a single scalar literal for a field per its type."""
    if ff.type == "string":
        return _odata_string(value)
    if ff.type == "boolean":
        if not isinstance(value, bool):
            raise SearchFilterError(f"Filter '{ff.index_field}' expects a boolean.")
        return "true" if value else "false"
    if ff.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SearchFilterError(f"Filter '{ff.index_field}' expects a number.")
        return str(value)
    if ff.type == "datetime":
        return _odata_datetime(value)
    raise SearchFilterError(f"Unsupported filter type for '{ff.index_field}'.")


def _compile_one(name: str, ff: FilterField, value: Any) -> str:
    """Compile one {field: value} entry to an OData clause."""
    field = ff.index_field

    # Range object: {"from": ..., "to": ...} -> ge / le (typical for datetimes).
    if isinstance(value, dict) and ("from" in value or "to" in value):
        parts: list[str] = []
        if value.get("from") is not None:
            parts.append(f"{field} ge {_render_scalar(ff, value['from'])}")
        if value.get("to") is not None:
            parts.append(f"{field} le {_render_scalar(ff, value['to'])}")
        if not parts:
            raise SearchFilterError(f"Range filter '{name}' had neither 'from' nor 'to'.")
        return "(" + " and ".join(parts) + ")"

    # List: OR of equality (only sensible for non-datetime scalars).
    if isinstance(value, list):
        if not value:
            raise SearchFilterError(f"Filter '{name}' was given an empty list.")
        if ff.type == "datetime":
            raise SearchFilterError(
                f"Filter '{name}' is a datetime; use a range object "
                "{'from': ..., 'to': ...} instead of a list."
            )
        clauses = [f"{field} eq {_render_scalar(ff, v)}" for v in value]
        return "(" + " or ".join(clauses) + ")"

    # Scalar equality.
    return f"{field} eq {_render_scalar(ff, value)}"


def compile_odata_filter(se: SearchEntity, filters: dict[str, Any] | None) -> str | None:
    """Compile a structured filter object to an OData ``$filter`` string.

    Args:
        se:      the target search entity (defines which fields are filterable).
        filters: ``{filter_field_name: value}``.  Values may be scalars, lists
                 (OR of equality), or range objects ``{"from": ..., "to": ...}``.

    Returns the compiled OData string, or None when there are no filters.

    Raises:
        SearchFilterError: unknown field or a value that doesn't match its type.
                           The tool surfaces this as a validation_error naming the
                           supported filter fields.
    """
    if not filters:
        return None

    clauses: list[str] = []
    for name, value in filters.items():
        if value is None:
            continue
        ff = se.filters.get(name)
        if ff is None:
            raise SearchFilterError(
                f"Unknown filter field '{name}' for entity '{se.entity}'. "
                f"Supported: {', '.join(se.filter_field_names())}."
            )
        clauses.append(_compile_one(name, ff, value))

    if not clauses:
        return None
    return " and ".join(clauses)
