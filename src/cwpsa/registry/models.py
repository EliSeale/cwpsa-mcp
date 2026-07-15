"""
Registry record Pydantic models (§5.2 of the architecture spec).

The registry is a compact JSON artifact built offline from the 11 MB OpenAPI spec.
It is loaded at startup and never reparsed at runtime.  Custom-field definitions
(the only runtime-merged part) are merged in by the reference cache at startup.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# Allowed CRUD operations per entity
Operation = Literal["query", "get", "count", "create", "update", "delete"]

# Broad field types; "custom_array" is the customFields envelope
FieldType = Literal["string", "integer", "float", "boolean", "enum", "ref", "text", "datetime", "custom_array", "array", "object"]


class FieldMeta(BaseModel):
    """Metadata for a single entity field."""

    type: FieldType
    filterable: bool = False
    sortable: bool = False
    required: bool = False

    # enum values (when type == "enum")
    values: list[str] = Field(default_factory=list)

    # ref fields
    ref_entity: str | None = None          # e.g. "service/boards"
    scoped_by: str | None = None           # e.g. "board" for status

    # custom fields use a separate conditions param
    filter_form: str | None = None         # e.g. "customFieldConditions"

    # projection rank (§14) — higher = more likely in default projection
    rank: float = 0.0

    # data-sensitivity (§5.3) — for RAG ACL in later phase
    visibility: Literal["customer_facing", "internal_only", "neutral"] = "neutral"


class CustomFieldDef(BaseModel):
    """A tenant-defined custom field definition (merged from live API at startup)."""

    id: int
    caption: str
    type: str                       # ConnectWise type string, e.g. "Text", "Date"
    entry_method: str | None = None
    number_of_decimals: int | None = None


class EntityRecord(BaseModel):
    """Registry entry for one ConnectWise entity."""

    entity: str                     # e.g. "service/tickets"
    id_field: str = "id"
    natural_identifier: str | None = None   # e.g. "summary", "name"

    operations: list[Operation] = Field(default_factory=list)
    default_projection: list[str] = Field(default_factory=list)

    fields: dict[str, FieldMeta] = Field(default_factory=dict)

    # Merged at runtime from system/userDefinedFields (§5.2)
    custom_fields: list[CustomFieldDef] = Field(default_factory=list)

    # Some entities use /search POST instead of GET for filtering
    supports_search_post: bool = True

    # Reporting endpoints use "columns" instead of "fields"
    uses_columns_param: bool = False

    # Raw extra metadata from the spec (version added, etc.)
    extra: dict[str, Any] = Field(default_factory=dict)

    def filterable_fields(self) -> list[str]:
        return [name for name, meta in self.fields.items() if meta.filterable]

    def sortable_fields(self) -> list[str]:
        return [name for name, meta in self.fields.items() if meta.sortable]


class Registry(BaseModel):
    """The full build-time registry artifact."""

    version: str = "0.0.0"
    spec_version: str = ""
    entities: dict[str, EntityRecord] = Field(default_factory=dict)

    # Domain ontology alias map (§5.3) — synonyms → canonical entity path
    # key: lowercased alias, value: canonical entity string (e.g. "company/contacts")
    alias_map: dict[str, str] = Field(default_factory=dict)

    def get_entity(self, entity: str) -> EntityRecord | None:
        return self.entities.get(entity)

    def resolve_alias(self, name: str) -> str | None:
        """Resolve an alias to a canonical entity path."""
        return self.alias_map.get(name.lower().strip())

    def entity_names(self) -> list[str]:
        return sorted(self.entities.keys())
