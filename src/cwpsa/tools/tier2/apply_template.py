"""
Tier 2 action tool — apply a project template to a project.

cw_apply_project_template applies a project template (a predefined set of phases,
tickets, and tasks) to an existing project. Additive, not destructive, but high
volume: one apply can add many records and is hard to cleanly undo, so it is
confirmation-gated and previews the blast radius (phase/ticket counts) first.

Endpoints:
  GET  /project/projectTemplates/                     list templates (choices)
  GET  /project/projectTemplates/{id}/projectTemplatePhases   preview phases
  POST /project/projects/{projectId}/applyTemplate/{templateId}  apply (no body)

The spec also exposes POST /project/projects/{id}/applyTemplates with an ARRAY
body for multi-apply, but the shared cw_post sends a dict, so multi-apply here
loops the bodyless single-template endpoint instead (one applyTemplate per
template). Functionally equivalent and avoids an array-body special case.

Write tool: mcp.write scope (auth=write_auth_check), CW_WRITES_DISABLED
kill-switch, runs under the caller's impersonated member. Registered in pep.py
_WRITE_TOOLS. Idempotency: before applying a template, its phases (by wbsCode)
are compared to the project's existing phases; if all are already present the
apply is skipped, so an agent retry does not stack a second copy of the workplan.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp import FastMCP

from cwpsa import config
from cwpsa.auth.pep import write_auth_check
from cwpsa.errors import ErrorEnvelope, not_found, upstream_error, validation_error, write_disabled
from cwpsa.integration.client import cw_count, cw_get as _cw_get, cw_post
from cwpsa.tools.tier2._action import item_result, needs_input, preview


def _as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    return [data] if data else []


async def _list_templates() -> list[dict[str, Any]]:
    data = await _cw_get("/project/projectTemplates/", fields="id,name,description", pageSize=200)
    return [{"id": t.get("id"), "name": t.get("name"), "description": t.get("description")}
            for t in _as_list(data)]


async def _template_wbs(template_id: int) -> set[str]:
    data = await _cw_get(f"/project/projectTemplates/{template_id}/projectTemplatePhases",
                         fields="id,wbsCode", pageSize=200)
    return {p.get("wbsCode") for p in _as_list(data) if p.get("wbsCode")}


async def _project_wbs(project_id: int) -> set[str]:
    data = await _cw_get(f"/project/projects/{project_id}/phases", fields="id,wbsCode", pageSize=200)
    return {p.get("wbsCode") for p in _as_list(data) if p.get("wbsCode")}


async def _template_summary(template_id: int) -> dict[str, Any]:
    """Name + phase/ticket counts, for the blast-radius preview."""
    name = None
    try:
        rec = await _cw_get(f"/project/projectTemplates/{template_id}", fields="id,name")
        name = (rec or {}).get("name")
    except Exception:  # noqa: BLE001
        pass
    try:
        phases = await cw_count(f"/project/projectTemplates/{template_id}/projectTemplatePhases")
    except Exception:  # noqa: BLE001
        phases = None
    try:
        tickets = await cw_count(f"/project/projectTemplates/{template_id}/projectTemplateTickets")
    except Exception:  # noqa: BLE001
        tickets = None
    return {"template_id": template_id, "name": name, "phase_count": phases, "ticket_count": tickets}


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": False},
        auth=write_auth_check,
    )
    async def cw_apply_project_template(
        project_id: int,
        template_id: int | None = None,
        template_ids: list[int] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Apply a project template (phases, tickets, tasks) to a project.

        Call with no template to see the available templates. Call with a chosen
        template and confirm=false to preview how many phases and tickets it will
        add. Call with confirm=true to apply. Applying is additive and hard to
        undo, so it is always confirmed. Re-applying a template whose phases are
        already on the project is skipped, so a retry will not duplicate the workplan.

        Args:
            project_id:    The target project.
            template_id:   A single template to apply.
            template_ids:  Several templates to apply (instead of template_id).
            confirm:       Must be true to apply.
        """
        if config.WRITES_DISABLED:
            return write_disabled()
        if template_id is not None and template_ids:
            return validation_error("Provide either template_id or template_ids, not both.")

        # Verify the project exists (and gives a clean not_found rather than a later 404).
        try:
            await _cw_get(f"/project/projects/{project_id}", fields="id,name")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return not_found(f"Project #{project_id} not found.")
            return upstream_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            return upstream_error(str(exc))

        chosen = template_ids or ([template_id] if template_id is not None else [])

        # 1. Nothing chosen -> surface the template list.
        if not chosen:
            return needs_input(
                "apply_project_template",
                missing=["template_id"],
                choices={"templates": await _list_templates()},
                next_hint="call again with template_id (or template_ids) and confirm=true to apply",
            )

        # 2. Preview -> blast radius per template.
        if not confirm:
            summaries = [await _template_summary(t) for t in chosen]
            total_phases = sum(s["phase_count"] or 0 for s in summaries)
            total_tickets = sum(s["ticket_count"] or 0 for s in summaries)
            return preview(
                "apply_project_template",
                will=(f"apply {len(chosen)} template(s) to project #{project_id}, adding about "
                      f"{total_phases} phase(s) and {total_tickets} ticket(s)"),
                project_id=project_id, templates=summaries,
            )

        # 3. Execute -> apply each template (bodyless), skipping any already present.
        existing = await _project_wbs(project_id)
        results: list[dict[str, Any]] = []
        for tid in chosen:
            try:
                twbs = await _template_wbs(tid)
                if twbs and twbs.issubset(existing):
                    results.append(item_result(tid, "apply_template", "skipped",
                                               "template phases already present on project"))
                    continue
                await cw_post(f"/project/projects/{project_id}/applyTemplate/{tid}", {})
                existing |= twbs  # avoid re-applying overlapping templates in the same call
                results.append(item_result(tid, "apply_template", "ok", "applied"))
            except httpx.HTTPStatusError as exc:
                results.append(item_result(tid, "apply_template", "error",
                                           f"{exc.response.status_code} {exc.response.text[:200]}"))
            except Exception as exc:  # noqa: BLE001
                results.append(item_result(tid, "apply_template", "error", str(exc)))

        applied = sum(1 for r in results if r["status"] == "ok")
        return {"status": "completed", "project_id": project_id, "results": results,
                "summary": f"{applied} of {len(chosen)} template(s) applied"}
