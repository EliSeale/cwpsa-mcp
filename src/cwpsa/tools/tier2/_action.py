"""
Shared helpers for Tier 2 action tools (action-tools design, prepare/execute).

Action tools return one of three intermediate/terminal shapes before or after a
write. These are plain dicts, not ErrorEnvelopes: a prepare or preview is a
normal, successful return that asks the caller for the next input or for
confirmation, not an error.

  needs_input  -> the action needs more input; carries the looked-up choices
  preview      -> everything is resolved; nothing written yet; confirm to run
  item_result  -> one per-record result line (used by orchestration tools)
"""

from __future__ import annotations

from typing import Any


def needs_input(
    action: str,
    missing: list[str],
    *,
    choices: dict[str, Any] | None = None,
    required_fields: dict[str, str] | None = None,
    next_hint: str | None = None,
) -> dict[str, Any]:
    """Prepare envelope: the action needs more input before it can run."""
    out: dict[str, Any] = {"status": "needs_input", "action": action, "missing": missing}
    if required_fields:
        out["required_fields"] = required_fields
    if choices:
        out["choices"] = choices
    if next_hint:
        out["next"] = next_hint
    return out


def preview(action: str, will: str, **details: Any) -> dict[str, Any]:
    """Confirmation preview: everything resolved, nothing written yet.

    The caller re-invokes with confirm=True to execute.
    """
    out: dict[str, Any] = {
        "status": "preview",
        "action": action,
        "will": will,
        "confirm_required": True,
    }
    out.update(details)
    return out


def item_result(
    record_id: int, action: str, status: str, detail: str | None = None
) -> dict[str, Any]:
    """A single per-record result line for orchestration/multi-write tools."""
    out: dict[str, Any] = {"record_id": record_id, "action": action, "status": status}
    if detail:
        out["detail"] = detail
    return out
