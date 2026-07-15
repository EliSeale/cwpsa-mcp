"""
cw_create / cw_update / cw_delete — Tier 1 mutation tools (§4.1, §4.5, §8.1).

Write safety (§8.1):
  - cw_create: idempotency_key deduplicates creates across retries.
  - cw_update: expected_version (lastUpdated) guards against stale edits.
  - cw_delete: requires explicit confirmation flag.

Write gating: all mutations check CW_WRITES_DISABLED kill-switch.
Tool annotations: split by verb so each carries accurate readOnly/destructive hints (§4.5).

TODO Phase 2:
  - Wire idempotency store (cache/idempotency.py) for cw_create.
  - Wire optimistic-concurrency re-fetch for cw_update.
  - Add PEP step-up check for cw_delete (§10.3).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cwpsa import config
from cwpsa.auth.pep import delete_auth_check, write_auth_check
from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error, write_disabled
from cwpsa.integration.client import cw_delete as _cw_delete, cw_patch, cw_post
from cwpsa.integration.patch_builder import build_patch
from cwpsa.registry.loader import get_registry

import httpx


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        auth=write_auth_check,
    )
    async def cw_create(
        entity: str,
        data: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Create a new ConnectWise record.

        Args:
            entity:          Entity path, e.g. "service/tickets".
            data:            Field values for the new record.  Required fields vary
                             by entity — call cw_describe(entity) to see required=true fields.
                             Reference fields: pass {id: <int>} or {identifier: "<str>"}.
                             Null values are omitted automatically.
            idempotency_key: Stable key to deduplicate retries.  If a create with the
                             same key already succeeded, the stored result is returned
                             instead of creating a duplicate.  Recommended for all creates.

        Returns the created record (id + full fields from ConnectWise).
        """
        if config.WRITES_DISABLED:
            return write_disabled()

        registry = get_registry()
        record = registry.get_entity(entity)
        if record is None:
            return validation_error(f"Unknown entity '{entity}'.")
        if "create" not in record.operations:
            return validation_error(f"Entity '{entity}' does not support create.")

        # TODO Phase 2: check idempotency store before proceeding

        try:
            result = await cw_post(f"/{entity}", data)
        except httpx.HTTPStatusError as e:
            return upstream_error(
                f"ConnectWise {e.response.status_code}: {e.response.text[:300]}"
            )
        except Exception as e:
            return upstream_error(str(e))

        # TODO Phase 2: store idempotency result

        return result

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        auth=write_auth_check,
    )
    async def cw_update(
        entity: str,
        id: int,
        operations: list[dict[str, Any]],
        expected_version: str | None = None,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Update a ConnectWise record using the ConnectWise patch dialect.

        ConnectWise uses its own PATCH format — not RFC 6902.
        Build operations with the helpers below (use integration/patch_builder.py
        directly for complex updates):

        Scalar field:
            {"op": "replace", "path": "summary", "value": "New title"}

        Reference field (replace whole object — NEVER use sub-paths):
            {"op": "replace", "path": "company", "value": {"identifier": "ACME"}}
            {"op": "replace", "path": "status",  "value": {"id": 7}}

        Custom fields (send ENTIRE array):
            {"op": "replace", "path": "customFields", "value": [...full array...]}

        Args:
            entity:           Entity path, e.g. "service/tickets".
            id:               Record ID to update.
            operations:       List of {op, path, value} patch operations.
            expected_version: The _version (lastUpdated) from a prior cw_get call.
                              Used for optimistic concurrency — if the record was
                              changed since you read it, the update is rejected with
                              a version_conflict error carrying the current state.
                              Strongly recommended; omitting it is an unguarded write.

        Returns the updated record.
        """
        if config.WRITES_DISABLED:
            return write_disabled()

        registry = get_registry()
        record = registry.get_entity(entity)
        if record is None:
            return validation_error(f"Unknown entity '{entity}'.")
        if "update" not in record.operations:
            return validation_error(f"Entity '{entity}' does not support update.")

        # Validate patch operations (no sub-paths)
        try:
            ops = build_patch(*operations)
        except ValueError as e:
            return validation_error(str(e))

        # TODO Phase 2: re-fetch + compare expected_version before patching

        try:
            result = await cw_patch(f"/{entity}/{id}", ops)
        except httpx.HTTPStatusError as e:
            return upstream_error(
                f"ConnectWise {e.response.status_code}: {e.response.text[:300]}"
            )
        except Exception as e:
            return upstream_error(str(e))

        return result

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        auth=delete_auth_check,
    )
    async def cw_delete(
        entity: str,
        id: int,
        confirm: bool = False,
    ) -> dict[str, Any] | ErrorEnvelope:
        """Delete a ConnectWise record.

        DESTRUCTIVE — requires confirm=true.

        Args:
            entity:  Entity path, e.g. "service/tickets".
            id:      Record ID to delete.
            confirm: Must be explicitly set to true to execute the delete.
                     This prevents accidental deletion from ambiguous intents.

        Returns {"deleted": true, "entity": "...", "id": <int>} on success.
        """
        if config.WRITES_DISABLED:
            return write_disabled()
        if not confirm:
            return validation_error(
                "confirm=true is required to delete a record. "
                "Confirm with the user before proceeding."
            )

        registry = get_registry()
        record = registry.get_entity(entity)
        if record is None:
            return validation_error(f"Unknown entity '{entity}'.")
        if "delete" not in record.operations:
            return validation_error(f"Entity '{entity}' does not support delete.")

        try:
            await _cw_delete(f"/{entity}/{id}")
        except httpx.HTTPStatusError as e:
            return upstream_error(
                f"ConnectWise {e.response.status_code}: {e.response.text[:300]}"
            )
        except Exception as e:
            return upstream_error(str(e))

        return {"deleted": True, "entity": entity, "id": id}
