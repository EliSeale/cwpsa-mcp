"""Filter DSL Pydantic models (§6 of the architecture spec).

The agent supplies a structured filter object; the compiler (compiler.py)
translates it to ConnectWise's proprietary conditions string language.
The agent NEVER writes raw conditions strings.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

# Supported filter operators
Op = Literal[
    "=", "!=", "<", "<=", ">", ">=",
    "contains", "not_contains",
    "like", "not_like",
    "in", "not_in",
]


class ConditionLeaf(BaseModel):
    """A single predicate: field op value."""

    field: str
    op: Op
    value: Any

    @model_validator(mode="after")
    def validate_in_value(self) -> "ConditionLeaf":
        if self.op in ("in", "not_in") and not isinstance(self.value, list):
            raise ValueError("'in'/'not_in' operator requires a list value")
        return self


class AndGroup(BaseModel):
    """Logical AND of child nodes."""

    and_: Annotated[list["ConditionNode"], Field(alias="and")]

    model_config = {"populate_by_name": True}


class OrGroup(BaseModel):
    """Logical OR of child nodes."""

    or_: Annotated[list["ConditionNode"], Field(alias="or")]

    model_config = {"populate_by_name": True}


# Recursive union — a node is a leaf, AND group, or OR group
ConditionNode = Annotated[
    Union[ConditionLeaf, AndGroup, OrGroup],
    Field(discriminator=None),
]

AndGroup.model_rebuild()
OrGroup.model_rebuild()


class OrderByClause(BaseModel):
    field: str
    dir: Literal["asc", "desc"] = "asc"


class FilterDSL(BaseModel):
    """
    Top-level filter DSL passed to cw_query / cw_count.

    Maps to ConnectWise query parameters:
      conditions              -> conditions=
      child_conditions        -> childConditions=
      custom_field_conditions -> customFieldConditions=
      order_by                -> orderBy=
      fields                  -> fields= (or columns= on reporting endpoints)
      page / page_size        -> page= / pageSize=

    Example:
    {
      "conditions": {
        "and": [
          {"field": "status/name",   "op": "=",    "value": "Open"},
          {"field": "priority/name", "op": "in",   "value": ["High", "Critical"]},
          {"field": "closedFlag",    "op": "=",    "value": false},
          {"field": "lastUpdated",   "op": ">=",   "value": "2026-06-01T00:00:00Z"}
        ]
      },
      "order_by": [{"field": "lastUpdated", "dir": "desc"}],
      "fields": ["id", "summary", "status/name", "company/name"],
      "page": 1,
      "page_size": 25
    }
    """

    conditions: ConditionNode | None = None
    child_conditions: ConditionNode | None = None
    custom_field_conditions: ConditionNode | None = None

    order_by: list[OrderByClause] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=1000)

    # Forward-only pagination (§8 pagination section)
    page_id: str | None = None

    @model_validator(mode="after")
    def validate_forward_only_order_by(self) -> "FilterDSL":
        if self.page_id and self.order_by:
            raise ValueError(
                "order_by is not allowed when using forward-only pagination (page_id). "
                "Forward-only mode sorts by id internally."
            )
        return self
