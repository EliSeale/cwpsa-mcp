"""
Registry loader — reads the pre-built registry.json artifact at startup.

The registry is produced offline by scripts/build_registry.py and shipped in
the container image.  It is never parsed from the raw 11 MB spec at runtime.

Usage:
    from cwpsa.registry.loader import get_registry
    registry = get_registry()          # cached singleton after first call
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from cwpsa.registry.models import Registry

log = logging.getLogger(__name__)

# Seed alias map (§5.3) — included here as a fallback when the artifact
# was built without the ontology step.  The build step should overwrite these.
_SEED_ALIAS_MAP: dict[str, str] = {
    # Contacts / people
    "client": "company/contacts",
    "customer": "company/contacts",
    "end user": "company/contacts",
    "prospect": "company/contacts",
    # Configurations
    "device": "company/configurations",
    "asset": "company/configurations",
    "configuration": "company/configurations",
    # Service boards
    "ticket queue": "service/boards",
    "work group": "service/boards",
    "board": "service/boards",
    # Members
    "user": "system/members",
    "employee": "system/members",
    "resource": "system/members",
    "technician": "system/members",
    "assigned technician": "system/members",
    # Finance
    "agreement": "finance/agreements",
    "contract": "finance/agreements",
    "invoice": "finance/invoices",
    "bill": "finance/invoices",
    "statement": "finance/invoices",
    # Sales
    "opportunity": "sales/opportunities",
    "quote": "sales/opportunities",
    # Company
    "company": "company/companies",
    "account": "company/companies",
    "site": "company/sites",
    "office": "company/sites",
    "customer location": "company/sites",
    # Tickets
    "ticket": "service/tickets",
    "service ticket": "service/tickets",
    "incident": "service/tickets",
    "request": "service/tickets",
    # Time
    "time entry": "time/entries",
    "timesheet": "time/entries",
    "time log": "time/entries",
}


@lru_cache(maxsize=1)
def get_registry(path: str | None = None) -> Registry:
    """Load and return the registry singleton.  Thread-safe after first call.

    Args:
        path: Override the registry artifact path.  Defaults to the value of
              CW_REGISTRY_PATH env var, then "registry.json" in CWD.
    """
    from cwpsa.config import REGISTRY_PATH

    artifact_path = Path(path or REGISTRY_PATH)

    if not artifact_path.exists():
        log.warning(
            "Registry artifact not found at '%s'. "
            "Run scripts/build_registry.py to generate it. "
            "Returning a minimal empty registry — most tools will degrade gracefully.",
            artifact_path,
        )
        registry = Registry()
    else:
        raw = artifact_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        registry = Registry.model_validate(data)
        log.info(
            "Registry loaded: %d entities, version=%s, spec=%s",
            len(registry.entities),
            registry.version,
            registry.spec_version,
        )

    # Merge seed alias map for entries not already present in the artifact
    for alias, entity in _SEED_ALIAS_MAP.items():
        registry.alias_map.setdefault(alias, entity)

    return registry


def invalidate_registry_cache() -> None:
    """Clear the lru_cache so get_registry() re-reads the artifact on next call."""
    get_registry.cache_clear()
