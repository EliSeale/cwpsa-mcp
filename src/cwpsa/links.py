"""
_links digest extraction — graph-style relation navigator (§4.4/§4.9).

Read tools (cw_get, cw_query, cw_follow_href) call `extract_links()` to walk
`_info` objects at every depth in a CW response, normalize API hrefs into a
bounded `_links` digest, and attach it to the response so the agent can
traverse related objects by relation name rather than by constructing URLs.

Shape (concise mode, default):
  { "rel": "company", "entity_hint": "company/companies", "id_hint": 42,
    "followable": true }

Shape (detailed mode, on request):
  { "rel": "company", "path": "company._info", "entity_hint": "company/companies",
    "id_hint": 42, "followable": true }

The raw href is never exposed in the digest (§4.9 / §10.5 untrusted-content rule).
link_ref opaque tokens (principal-bound, expiry-scoped) are a Phase 5 addition.

Validation:
  Only ConnectWise API hrefs (correct host + /v4_6_release/apis/3.0/ path prefix)
  are included.  Free-text URL fields are not walked — only `_info` dicts.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_PATH_SEGMENT = "/v4_6_release/apis/3.0"
_MAX_LINKS = 15        # cap per response to stay within §4.4 budget
_HREF_KEY_RE = re.compile(r"^(href_|.*_href)$")   # matches href_xxx or xxx_href

# Fields that are NOT link containers — skip to avoid false positives
_SKIP_KEYS = frozenset(["lastUpdated", "updatedBy", "enteredBy", "mobileGuid",
                         "guid", "dateEntered"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_links(
    data: Any,
    cw_base_url: str,
    response_format: str = "concise",
) -> list[dict[str, Any]]:
    """Walk a CW response and return a bounded `_links` digest.

    Args:
        data:            CW API response (dict or list of dicts).
        cw_base_url:     Configured CW base URL (for host validation).
        response_format: "concise" (rel + entity_hint + id_hint) or
                         "detailed" (adds path field).

    Returns a list of at most `_MAX_LINKS` link entries.
    """
    allowed_host = urlparse(cw_base_url).netloc.lower()
    links: list[dict[str, Any]] = []

    if isinstance(data, list):
        # For lists, walk only the first record (others have the same relation shape)
        if data and isinstance(data[0], dict):
            _walk(data[0], path=[], allowed_host=allowed_host, links=links,
                  detailed=(response_format == "detailed"))
    elif isinstance(data, dict):
        _walk(data, path=[], allowed_host=allowed_host, links=links,
              detailed=(response_format == "detailed"))

    return links[:_MAX_LINKS]


def attach_links(
    response: dict[str, Any],
    cw_base_url: str,
    response_format: str = "concise",
) -> dict[str, Any]:
    """Attach `_links` to a response dict in-place and return it.

    Skips if `_links` would be empty (no API hrefs found).
    Strips raw `_info` blobs from the response in concise mode so audit
    noise (lastUpdated, updatedBy, guids) doesn't reach the model.
    """
    links = extract_links(response, cw_base_url, response_format)
    if links:
        response["_links"] = links
    # Remove raw _info in concise mode (audit noise; version token already surfaced
    # as _version by cw_get)
    if response_format == "concise" and "_info" in response:
        del response["_info"]
    return response


# ---------------------------------------------------------------------------
# Internal walker
# ---------------------------------------------------------------------------

def _walk(
    obj: dict[str, Any],
    path: list[str],
    allowed_host: str,
    links: list[dict[str, Any]],
    detailed: bool,
) -> None:
    """Recursively walk a dict, extracting hrefs from `_info` sub-objects."""
    if len(links) >= _MAX_LINKS:
        return

    # Extract hrefs from this object's _info
    info = obj.get("_info")
    if isinstance(info, dict):
        _extract_from_info(info, path, allowed_host, links, detailed)

    # Recurse into nested reference objects (not arrays, not _info)
    for key, value in obj.items():
        if key in ("_info", "_links", "_version") or key in _SKIP_KEYS:
            continue
        if isinstance(value, dict) and len(links) < _MAX_LINKS:
            _walk(value, path + [key], allowed_host, links, detailed)


def _extract_from_info(
    info: dict[str, Any],
    parent_path: list[str],
    allowed_host: str,
    links: list[dict[str, Any]],
    detailed: bool,
) -> None:
    """Extract href entries from a single `_info` dict."""
    info_path = ".".join(parent_path + ["_info"]) if parent_path else "_info"

    for key, value in info.items():
        if not isinstance(value, str):
            continue
        if key in _SKIP_KEYS:
            continue
        # Only process keys that look like href entries
        if not _is_href_key(key):
            continue
        # Validate that the value is a ConnectWise API URL
        if not _is_api_href(value, allowed_host):
            continue

        rel = _normalize_rel(key, parent_path)
        entity_hint = _map_href_to_entity(value)
        id_hint = _extract_id_from_href(value)

        entry: dict[str, Any] = {
            "rel": rel,
            "entity_hint": entity_hint,
            "id_hint": id_hint,
            "followable": True,
        }
        if detailed:
            entry["path"] = info_path

        links.append(entry)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_href_key(key: str) -> bool:
    """Return True if this key looks like a href field."""
    k = key.lower()
    return k.endswith("_href") or k.startswith("href_") or k.endswith("href")


def _is_api_href(value: str, allowed_host: str) -> bool:
    """Return True if the value is a ConnectWise API href targeting our host."""
    try:
        parsed = urlparse(value)
        if not parsed.netloc:
            return False
        if parsed.netloc.lower() != allowed_host:
            return False
        return _API_PATH_SEGMENT in parsed.path
    except Exception:
        return False


def _normalize_rel(key: str, parent_path: list[str]) -> str:
    """Strip href_/  _href affixes to produce a stable relation name."""
    rel = key
    # Strip href_ prefix
    if rel.lower().startswith("href_"):
        rel = rel[5:]
    # Strip _href suffix
    if rel.lower().endswith("_href"):
        rel = rel[:-5]
    # If the rel name would be blank or just noise, use parent path context
    rel = rel.strip("_").strip()
    if not rel and parent_path:
        rel = parent_path[-1]
    return rel or key


def _map_href_to_entity(href: str) -> str | None:
    """Extract a registry entity path from a CW API href."""
    try:
        path = urlparse(href).path
        if _API_PATH_SEGMENT in path:
            api_path = path[path.index(_API_PATH_SEGMENT) + len(_API_PATH_SEGMENT):]
        else:
            api_path = path

        # Strip leading slash, split, drop numeric segments from the end
        parts = api_path.strip("/").split("/")
        # Walk from longest to shortest to find a known entity
        from cwpsa.registry.loader import get_registry
        registry = get_registry()
        for length in range(len(parts), 0, -1):
            candidate = "/".join(parts[:length])
            if candidate.split("/")[-1].isdigit():
                continue
            if registry.get_entity(candidate):
                return candidate
        return None
    except Exception:
        return None


def _extract_id_from_href(href: str) -> int | None:
    """Extract the last numeric path segment as an id hint."""
    try:
        path = urlparse(href).path.rstrip("/")
        last = path.split("/")[-1]
        if last.isdigit():
            return int(last)
        return None
    except Exception:
        return None
