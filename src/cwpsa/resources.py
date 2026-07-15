"""
MCP Resources — read-only, app-surfaced context (§4.6).

Exposes the registry entity catalog and cached reference data as MCP Resources
so a host can surface them as context without spending a tool round-trip.

Resources:
  cw://registry/entities          — names-only entity catalog
  cw://registry/entity/{entity}   — cw_describe manifest for one entity
  cw://reference/boards           — live service boards
  cw://reference/priorities       — live ticket priorities
  cw://reference/statuses/{board} — live board-scoped statuses
"""

from __future__ import annotations

import json
import logging

from fastmcp import FastMCP

from cwpsa.registry.loader import get_registry
from cwpsa.resolution.cache import get_boards, get_priorities

log = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register MCP Resources on the server."""

    @mcp.resource("cw://registry/entities")
    async def entity_catalog() -> str:
        """Names-only catalog of all ConnectWise entities available through this server."""
        registry = get_registry()
        return json.dumps({"entities": registry.entity_names()}, indent=2)

    @mcp.resource("cw://reference/boards")
    async def service_boards() -> str:
        """Live list of service boards in this ConnectWise instance."""
        boards = await get_boards()
        return json.dumps({"boards": boards}, indent=2)

    @mcp.resource("cw://reference/priorities")
    async def ticket_priorities() -> str:
        """Live list of ticket priorities in this ConnectWise instance."""
        priorities = await get_priorities()
        return json.dumps({"priorities": priorities}, indent=2)
