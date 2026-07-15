"""
ConnectWise patch dialect builder (§8 updates).

ConnectWise uses its OWN patch format — NOT RFC 6902:
  - Array of {op, path, value}
  - ops: "add" | "replace" | "remove"
  - path: bare, case-sensitive field name  e.g. "summary"  (NOT "/summary")
  - References: replace the WHOLE object with a unique identifier value
    e.g. {"op": "replace", "path": "company", "value": {"identifier": "ACME"}}
    NEVER a sub-path like "company/identifier" (can produce a false 200)
  - Custom fields: send the ENTIRE customFields array, never a single field
  - Scalars: {"op": "replace", "path": "summary", "value": "New title"}

Build patch operations with the helpers below — never construct them manually.
"""

from __future__ import annotations

from typing import Any


PatchOp = dict[str, Any]  # {op: str, path: str, value: Any}


def scalar(field: str, value: Any, op: str = "replace") -> PatchOp:
    """A scalar field update.

    Args:
        field: Bare field name, e.g. "summary", "billableOption".
        value: The new value.  Enums as strings; datetimes as ISO-8601 UTC strings.
        op: "replace" (default), "add", or "remove".
    """
    if op not in ("add", "replace", "remove"):
        raise ValueError(f"Invalid patch op '{op}'. Must be add, replace, or remove.")
    patch: PatchOp = {"op": op, "path": field}
    if op != "remove":
        patch["value"] = value
    return patch


def reference(field: str, identifier: str | None = None, id_: int | None = None) -> PatchOp:
    """A reference field update — replaces the whole ref object (§8 rule).

    Pass EITHER identifier OR id_ (not both).  identifier is preferred when
    available because it's human-readable and ConnectWise allows matching by it.

    Example:
        reference("company", identifier="ACME")
        -> {"op": "replace", "path": "company", "value": {"identifier": "ACME"}}

        reference("status", id_=7)
        -> {"op": "replace", "path": "status", "value": {"id": 7}}
    """
    if identifier is not None and id_ is not None:
        raise ValueError("Provide either identifier or id_, not both.")
    if identifier is None and id_ is None:
        raise ValueError("Provide either identifier or id_.")
    value = {"identifier": identifier} if identifier is not None else {"id": id_}
    return {"op": "replace", "path": field, "value": value}


def custom_fields(fields_array: list[dict[str, Any]]) -> PatchOp:
    """Replace the entire customFields array (§8 rule — never patch a single field).

    `fields_array` should be the full customFields array as returned by a prior
    cw_get call, with the target field's value updated in place.
    """
    return {"op": "replace", "path": "customFields", "value": fields_array}


def build_patch(*operations: PatchOp) -> list[PatchOp]:
    """Collect and validate patch operations into a list ready for PATCH body.

    Performs a basic sanity check: rejects sub-path references like
    "company/identifier" (the most common false-200 trap in §8).
    """
    for op in operations:
        path = op.get("path", "")
        if "/" in path:
            raise ValueError(
                f"Patch path '{path}' contains a '/'.  ConnectWise patch paths must be "
                "bare field names (e.g. 'company'), not sub-paths (e.g. 'company/identifier'). "
                "Use the reference() helper to replace the whole ref object."
            )
    return list(operations)
