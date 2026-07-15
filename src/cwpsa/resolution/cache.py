"""
Reference data cache — tenant-specific boards, statuses, types, priorities (§11).

Populated at startup and held in-process with a long TTL.  Invalidated by
ConnectWise callbacks (§8) or on MCP notifications/tools/list_changed.

TODO Phase 2: wire callback-triggered invalidation via /system/callbacks.
Current implementation: simple in-process TTL dict suitable for Phase 1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from cwpsa.integration.client import cw_get

log = logging.getLogger(__name__)

# Cache TTL in seconds (1 hour default — boards/statuses change rarely)
DEFAULT_TTL = 3600.0

_store: dict[str, tuple[list[dict[str, Any]], float]] = {}
_lock = asyncio.Lock()


async def get_or_fetch(key: str, endpoint: str, ttl: float = DEFAULT_TTL) -> list[dict[str, Any]]:
    """Return cached reference data or fetch and cache it."""
    async with _lock:
        entry = _store.get(key)
        if entry and (time.monotonic() - entry[1]) < ttl:
            return entry[0]
        try:
            data = await cw_get(endpoint, fields="id,name", pageSize=1000)
            if isinstance(data, list):
                _store[key] = (data, time.monotonic())
                log.debug("Reference cache: fetched %d items for '%s'", len(data), key)
                return data
        except Exception as exc:
            log.warning("Reference cache: failed to fetch '%s': %s", key, exc)
            if entry:
                return entry[0]  # serve stale on error
    return []


async def get_boards() -> list[dict[str, Any]]:
    return await get_or_fetch("boards", "/service/boards")


async def get_priorities() -> list[dict[str, Any]]:
    return await get_or_fetch("priorities", "/service/priorities")


async def get_slas() -> list[dict[str, Any]]:
    return await get_or_fetch("slas", "/service/SLAs")


async def get_company_statuses() -> list[dict[str, Any]]:
    return await get_or_fetch("company_statuses", "/company/companies/statuses")


async def get_company_types() -> list[dict[str, Any]]:
    return await get_or_fetch("company_types", "/company/companies/types")


async def warm_cache() -> str:
    """Pre-warm all reference sets.  Called at startup (§12.2)."""
    lines: list[str] = []
    for label, coro in [
        ("Service boards", get_boards()),
        ("Ticket priorities", get_priorities()),
        ("SLAs", get_slas()),
        ("Company statuses", get_company_statuses()),
        ("Company types", get_company_types()),
    ]:
        try:
            items = await coro
            names = [r["name"] for r in items if r.get("name")]
            if names:
                lines.append(f"- {label}: {', '.join(names)}")
        except Exception as exc:
            log.warning("[vocab] skipped %s: %s", label, exc)

    log.info("[vocab] loaded %d reference set(s)", len(lines))
    return "\n".join(lines)


def invalidate(key: str | None = None) -> None:
    """Invalidate one or all cache entries."""
    if key:
        _store.pop(key, None)
    else:
        _store.clear()
