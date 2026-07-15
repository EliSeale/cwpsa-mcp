"""
Filter DSL validator — validates a FilterDSL against the registry before any API call (§7).

Pre-flight validation turns cryptic ConnectWise 400s into self-correcting errors
with did-you-mean suggestions the agent can act on immediately.

All validation rules are derived from the registry (deterministic) — no hardcoded
field lists or allowed values.

TODO: implement full validation logic.  Skeleton tracks all required checks.
"""

from __future__ import annotations

import difflib

from cwpsa.errors import ErrorEnvelope, validation_error
from cwpsa.filter import ConditionLeaf, ConditionNode, FilterDSL, AndGroup, OrGroup
from cwpsa.registry.models import EntityRecord


def validate_filter(dsl: FilterDSL, entity: EntityRecord) -> ErrorEnvelope | None:
    """Validate a FilterDSL against an entity's registry record.

    Returns None if valid, or an ErrorEnvelope with a corrective message if not.
    The caller should return the envelope directly without calling the API.

    Checks performed:
      1. All `field` references in conditions exist and are marked filterable.
      2. Enum fields: value is in the allowed set.
      3. `order_by` fields exist and are marked sortable.
      4. `fields` (projection) references exist.
      5. `in`/`not_in` appear only in conditions (enforced by compiler too, belt-and-suspenders).
    """
    errors: list[str] = []
    suggestions: list[str] = []
    allowed_values: list[str] = []

    # --- conditions ---
    if dsl.conditions is not None:
        _validate_condition_node(dsl.conditions, entity, "conditions", errors, suggestions, allowed_values)

    # --- child_conditions ---
    if dsl.child_conditions is not None:
        _validate_condition_node(dsl.child_conditions, entity, "child_conditions", errors, suggestions, allowed_values)

    # --- order_by ---
    for clause in dsl.order_by:
        if clause.field not in entity.fields:
            close = difflib.get_close_matches(clause.field, entity.sortable_fields(), n=3, cutoff=0.6)
            errors.append(f"order_by field '{clause.field}' not found on {entity.entity}.")
            suggestions.extend(close)
        elif not entity.fields[clause.field].sortable:
            errors.append(f"Field '{clause.field}' on {entity.entity} is not sortable.")

    # --- projection fields ---
    for f in dsl.fields:
        # Allow reference traversal like "status/name" — validate the root field only
        root = f.split("/")[0]
        if root not in entity.fields:
            close = difflib.get_close_matches(root, entity.filterable_fields(), n=3, cutoff=0.6)
            errors.append(f"Projection field '{f}' not found on {entity.entity}.")
            suggestions.extend(close)

    if errors:
        msg = "; ".join(errors)
        return validation_error(
            message=msg,
            suggestions=list(dict.fromkeys(suggestions)),  # dedup, preserve order
            allowed_values=list(dict.fromkeys(allowed_values)),
        )

    return None


def _validate_condition_node(
    node: ConditionNode,
    entity: EntityRecord,
    context: str,
    errors: list[str],
    suggestions: list[str],
    allowed_values: list[str],
) -> None:
    """Recursively walk a condition node and collect validation errors."""
    if isinstance(node, ConditionLeaf):
        _validate_leaf(node, entity, context, errors, suggestions, allowed_values)
    elif isinstance(node, AndGroup):
        for child in node.and_:
            _validate_condition_node(child, entity, context, errors, suggestions, allowed_values)
    elif isinstance(node, OrGroup):
        for child in node.or_:
            _validate_condition_node(child, entity, context, errors, suggestions, allowed_values)


def _validate_leaf(
    leaf: ConditionLeaf,
    entity: EntityRecord,
    context: str,
    errors: list[str],
    suggestions: list[str],
    allowed_values: list[str],
) -> None:
    """Validate a single predicate leaf against the entity schema."""
    # Allow reference traversal like "company/identifier" — check root field
    root = leaf.field.split("/")[0]

    if root not in entity.fields:
        field_names = list(entity.fields.keys())
        close = difflib.get_close_matches(root, field_names, n=3, cutoff=0.6)
        errors.append(
            f"Unknown field '{leaf.field}' on {entity.entity}."
            + (f" Did you mean: {', '.join(repr(c) for c in close)}?" if close else "")
        )
        suggestions.extend(close)
        return

    field_meta = entity.fields[root]

    if not field_meta.filterable:
        errors.append(
            f"Field '{leaf.field}' on {entity.entity} is not filterable "
            "(it is not present in the GET response). Use a different field."
        )

    # Enum value validation
    if field_meta.type == "enum" and field_meta.values:
        values_to_check = leaf.value if isinstance(leaf.value, list) else [leaf.value]
        for v in values_to_check:
            if isinstance(v, str) and v not in field_meta.values:
                close = difflib.get_close_matches(v, field_meta.values, n=3, cutoff=0.6)
                errors.append(
                    f"Invalid value '{v}' for enum field '{leaf.field}' on {entity.entity}."
                    + (f" Did you mean: {', '.join(repr(c) for c in close)}?" if close else "")
                )
                allowed_values.extend(field_meta.values)
