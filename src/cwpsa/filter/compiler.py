"""
Filter DSL → ConnectWise conditions string compiler (§6 of the architecture spec).

Correctness-critical transforms (§13.1):
  - Every operator maps to the exact CW form.
  - Values are formatted by type: strings double-quoted+escaped, datetimes in
    [UTC brackets] with offset rejection, booleans as True/False, nulls bare,
    numbers bare, lists as (v1, v2, ...) for `in`.
  - Conditions-injection prevention: user values are never string-concatenated
    raw — they flow through typed formatting only (§6.1 named control).
  - `in`/`not_in` are rejected in child_conditions and custom_field_conditions.
  - `order_by` is rejected when forward-only page_id is present.
  - URLs exceeding ~10,000 chars or containing `*` or many encoded chars should
    use the POST /search body path (handled by integration/client.py caller).

Unit tests: tests/unit/test_compiler.py — golden tests + property tests cover
every operator, nesting, value type, URL-encoding, and injection invariant.
"""

from __future__ import annotations

import re
from typing import Any

from cwpsa.filter import AndGroup, ConditionLeaf, ConditionNode, FilterDSL, OrderByClause, OrGroup

# ---------------------------------------------------------------------------
# Operator maps
# ---------------------------------------------------------------------------
_OP_MAP: dict[str, str] = {
    "=": "=",
    "!=": "!=",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "contains": "contains",
    "not_contains": "not contains",
    "like": "like",
    "not_like": "not like",
    "in": "in",
    "not_in": "not in",
}

# Operators disallowed in childConditions and customFieldConditions (§6.1)
_NO_CHILD_OPS = frozenset(["in", "not_in"])

# Datetime pattern — UTC, no offset: 2026-06-01T00:00:00Z
_DT_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Offset-bearing datetime — explicitly rejected
_DT_OFFSET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

def _format_value(v: Any) -> str:
    """Format a filter value per ConnectWise rules (§6.1).

    - None  -> null
    - bool  -> True / False
    - int/float -> bare number
    - str (datetime UTC) -> [2026-06-01T00:00:00Z]
    - str (other) -> "double-quoted, escaped"

    Raises ValueError on offset-bearing datetimes (§6.1 hard rule).
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        if _DT_OFFSET_RE.match(v):
            raise ValueError(
                f"Datetime value '{v}' has a timezone offset. "
                "ConnectWise requires UTC datetimes with no offset (e.g. '2026-06-01T00:00:00Z'). "
                "Please convert to UTC and remove the offset."
            )
        if _DT_UTC_RE.match(v):
            return f"[{v}]"
        # String: double-quote + escape internal double-quotes and backslashes.
        # Single-quotes are valid inside a double-quoted string; no escaping needed.
        # Newline/CR are not supported by CW JSON — collapse to space.
        safe = (
            v.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", " ")
             .replace("\r", " ")
        )
        return f'"{safe}"'
    # Fallback for unexpected types
    return f'"{v}"'


def _format_in_list(values: list[Any]) -> str:
    """Format a list for the `in (...)` operator."""
    return "(" + ", ".join(_format_value(v) for v in values) + ")"


# ---------------------------------------------------------------------------
# Node compiler (recursive)
# ---------------------------------------------------------------------------

def _compile_node(node: ConditionNode, context: str = "conditions") -> str:
    """Recursively compile a condition tree node to a CW conditions string segment."""
    if isinstance(node, ConditionLeaf):
        return _compile_leaf(node, context)
    if isinstance(node, AndGroup):
        children = [_compile_node(n, context) for n in node.and_]
        if len(children) == 1:
            return children[0]
        return "(" + " and ".join(children) + ")"
    if isinstance(node, OrGroup):
        children = [_compile_node(n, context) for n in node.or_]
        if len(children) == 1:
            return children[0]
        return "(" + " or ".join(children) + ")"
    raise TypeError(f"Unknown condition node type: {type(node)}")


def _compile_leaf(leaf: ConditionLeaf, context: str) -> str:
    """Compile a single predicate leaf, enforcing context rules."""
    op = leaf.op

    # `in`/`not_in` is valid only on conditions, not child/custom (§6.2)
    if op in _NO_CHILD_OPS and context != "conditions":
        raise ValueError(
            f"Operator '{op}' is only valid in 'conditions', "
            f"not in '{context}'. Use '=' or 'contains' for child/custom field filters."
        )

    cw_op = _OP_MAP[op]
    field = leaf.field

    if op in ("in", "not_in"):
        if not isinstance(leaf.value, list):
            raise ValueError(f"Operator '{op}' requires a list value.")
        return f"{field} {cw_op} {_format_in_list(leaf.value)}"

    return f"{field} {cw_op} {_format_value(leaf.value)}"


# ---------------------------------------------------------------------------
# Order-by compiler
# ---------------------------------------------------------------------------

def _compile_order_by(clauses: list[OrderByClause]) -> str:
    parts = [f"{c.field} {c.dir}" for c in clauses]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Custom-field condition compiler
# ---------------------------------------------------------------------------
# Custom field clauses compile to: caption="X" AND value <op> <val>

def _compile_custom_field_node(node: ConditionNode) -> str:
    """Compile a custom-field condition tree.

    CW expects: caption="Field Name" AND value <op> <val>
    For AND groups, each leaf is compiled independently and joined with AND.
    """
    if isinstance(node, ConditionLeaf):
        # For custom fields the `field` attribute is the caption name
        caption_safe = node.field.replace('"', '\\"')
        val_str = _format_value(node.value)
        cw_op = _OP_MAP.get(node.op, node.op)
        return f'caption="{caption_safe}" AND value {cw_op} {val_str}'
    if isinstance(node, AndGroup):
        parts = [_compile_custom_field_node(n) for n in node.and_]
        return " AND ".join(parts)
    if isinstance(node, OrGroup):
        parts = [_compile_custom_field_node(n) for n in node.or_]
        return " OR ".join(parts)
    raise TypeError(f"Unknown custom field node: {type(node)}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class CompiledQuery:
    """Output of compile_filter — ready-to-use httpx params dict."""

    def __init__(self) -> None:
        self.params: dict[str, str | int] = {}
        self.use_search_post: bool = False  # caller should POST to /search if True

    def _set_conditions(self, key: str, value: str) -> None:
        self.params[key] = value
        # Recommend /search POST if the string is long or contains encoded-heavy chars
        total_len = sum(len(str(v)) for v in self.params.values())
        if total_len > 9000 or "*" in value:
            self.use_search_post = True


def compile_filter(dsl: FilterDSL) -> CompiledQuery:
    """Compile a FilterDSL object to a ConnectWise query params dict.

    Returns a CompiledQuery whose `.params` can be passed directly to httpx
    and whose `.use_search_post` flag signals the caller to switch to POST /search.

    Raises ValueError on invalid DSL (bad operators in context, offset datetimes, etc.).
    """
    q = CompiledQuery()

    if dsl.conditions is not None:
        q._set_conditions("conditions", _compile_node(dsl.conditions, "conditions"))

    if dsl.child_conditions is not None:
        q._set_conditions(
            "childConditions", _compile_node(dsl.child_conditions, "child_conditions")
        )

    if dsl.custom_field_conditions is not None:
        q._set_conditions(
            "customFieldConditions",
            _compile_custom_field_node(dsl.custom_field_conditions),
        )

    if dsl.order_by:
        q.params["orderBy"] = _compile_order_by(dsl.order_by)

    if dsl.fields:
        q.params["fields"] = ",".join(dsl.fields)

    if dsl.page_id:
        q.params["pageId"] = dsl.page_id
    else:
        q.params["page"] = dsl.page
        q.params["pageSize"] = dsl.page_size

    return q
