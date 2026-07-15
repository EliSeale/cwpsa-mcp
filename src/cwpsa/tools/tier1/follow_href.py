"""
cw_follow_href -- Tier 1 tool: safe ConnectWise API href follower (§4.9).

Follows a ConnectWise API href that was returned in an _info object or reference
metadata.  This is an escape hatch for cases where a useful resource is only
reachable via a server-supplied href -- not the primary navigation path.

The agent should normally use cw_get, cw_query, cw_resolve, and workflow tools.
Use cw_follow_href only when ConnectWise returns an _info href that those tools
cannot easily reach (e.g. teams_href, sites_href, contacts_href, notes_href).

Validation rules enforced (§4.9):
  1. Host allowlist     -- must match the configured CW API host
  2. API path allowlist -- path must be under the configured CW API base
  3. GET only           -- never mutates
  4. Registry mapping   -- maps to known entity/projection where possible
  5. PEP enforcement    -- handled at server level (tool is read-only annotated)
  6. Query sanitization -- strips all non-allowlisted query parameters
  7. Response governance -- same budget + has_more envelope as cw_get/cw_query
  8. No credential leakage -- raw upstream URL is never echoed back in full

Phase 2 addition: link_ref opaque tokens (server-minted, principal-bound, expiry-scoped).
For now, raw href input is accepted and validated against the same rules.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from fastmcp import FastMCP

from cwpsa import config
from cwpsa.errors import ErrorEnvelope, upstream_error, validation_error

# Query parameters that are safe to forward from a href to the upstream call
_SAFE_QUERY_PARAMS = frozenset([
    "conditions", "childConditions", "customFieldConditions",
    "orderBy", "fields", "page", "pageSize", "pageId",
])

# The versioned API path prefix that all CW REST endpoints share
_API_PATH_SEGMENT = "/v4_6_release/apis/3.0"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _allowed_host(parsed_url: Any) -> bool:
    """Return True if the URL's host matches the configured CW API host."""
    configured = urlparse(config.CW_BASE_URL)
    return parsed_url.netloc.lower() == configured.netloc.lower()


def _allowed_path(path: str) -> bool:
    """Return True if the path is under the CW REST API base path."""
    normalized = path.rstrip("/")
    return normalized.startswith(_API_PATH_SEGMENT + "/") or normalized == _API_PATH_SEGMENT


def _strip_to_api_path(href: str) -> str:
    """Extract just the API path from a full href, stripping host + base prefix."""
    parsed = urlparse(href)
    path = parsed.path
    # Strip the versioned prefix so we get e.g. /service/tickets/123/notes
    if _API_PATH_SEGMENT in path:
        return path[path.index(_API_PATH_SEGMENT) + len(_API_PATH_SEGMENT):]
    return path


def _sanitize_query_params(href: str) -> dict[str, Any]:
    """Return only the allowlisted query parameters from the href."""
    parsed = urlparse(href)
    raw = parse_qs(parsed.query, keep_blank_values=False)
    safe: dict[str, Any] = {}
    for key, values in raw.items():
        if key in _SAFE_QUERY_PARAMS and values:
            safe[key] = values[0]  # take first value
    return safe


def _validate_href(href: str) -> ErrorEnvelope | None:
    """Run all validation rules.  Returns an ErrorEnvelope if any rule fails."""
    if not href or not href.strip():
        return validation_error("href must not be empty.")

    # Must be a parseable URL with a scheme and netloc
    try:
        parsed = urlparse(href.strip())
    except Exception:
        return validation_error(f"Could not parse href: {href!r}")

    if not parsed.scheme or not parsed.netloc:
        # Bare path — accept it as a relative API path for convenience
        if not href.startswith("/"):
            return validation_error(
                "href must be a full ConnectWise API URL or a bare API path starting with '/'."
            )
        # Validate it's under the API prefix
        if not _allowed_path(href.split("?")[0]):
            return validation_error(
                f"Path '{href.split('?')[0]}' is not under the ConnectWise REST API path "
                f"('{_API_PATH_SEGMENT}/...'). Only ConnectWise API paths are followable.",
            )
        return None  # bare path passes

    # Rule 1: Host allowlist
    if not _allowed_host(parsed):
        configured_host = urlparse(config.CW_BASE_URL).netloc
        return validation_error(
            f"href host '{parsed.netloc}' does not match the configured ConnectWise "
            f"API host '{configured_host}'. External URLs, payment links, remote-control "
            "links, and arbitrary customer URLs cannot be followed."
        )

    # Rule 2: API path allowlist
    if not _allowed_path(parsed.path):
        return validation_error(
            f"href path '{parsed.path}' is not under the ConnectWise REST API path "
            f"('{_API_PATH_SEGMENT}/...'). Only CW API paths are followable."
        )

    return None


def _map_to_entity(api_path: str) -> str | None:
    """Attempt to map an API path to a registry entity path.

    e.g. /service/tickets/123 -> service/tickets
         /company/companies/456/teams -> company/companies (parent entity)
    """
    from cwpsa.registry.loader import get_registry
    registry = get_registry()

    # Strip leading slash and trailing numeric ID / sub-resource segments
    path = api_path.strip("/")

    # Try progressively shorter paths until we find a known entity
    parts = path.split("/")
    for length in range(len(parts), 0, -1):
        candidate = "/".join(parts[:length])
        # Skip pure-numeric segments (they're record IDs, not entity names)
        if candidate.split("/")[-1].isdigit():
            continue
        if registry.get_entity(candidate):
            return candidate

    return None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp: FastMCP) -> None:

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
        }
    )
    async def cw_follow_href(
        href: str,
        fields: list[str] | None = None,
        response_format: str = "concise",
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any] | list[Any] | ErrorEnvelope:
        """Fetch a ConnectWise API resource via a server-supplied href.

        Use this tool when ConnectWise returns an _info href (such as teams_href,
        sites_href, contacts_href, tickets_href, notes_href) and you need to follow
        it.  This is an escape hatch -- prefer cw_get, cw_query, and workflow tools
        for normal navigation.

        How to find followable hrefs:
          1. Call cw_get or any workflow tool that returns a record.
          2. Look inside the _info object for keys ending in '_href'.
          3. Pass the href value directly to this tool.

        Example:
          cw_get("company/companies", 123) returns _info.teams_href.
          cw_follow_href("https://connect.verveit.com/.../company/companies/123/teams")
          -> returns team members for that company.

        Args:
            href:            Full ConnectWise API URL or bare API path starting with '/'.
                             Must target the configured ConnectWise host and be under
                             the REST API base path. External URLs are rejected.
            fields:          Optional list of fields to return (same as cw_get).
            response_format: "concise" (default, governed slice) or "detailed" (all fields).
            page:            Page number for paginated results (default 1).
            page_size:       Records per page (default 25, max 1000).

        Security:
            - Only GET is performed. No mutations are possible via this tool.
            - Unknown query parameters from the href are stripped.
            - The target host must match the configured ConnectWise API host.
            - The path must be under /v4_6_release/apis/3.0/...
            - The authenticated principal must be authorized (PEP is applied).
        """
        href = href.strip()

        # Validate
        err = _validate_href(href)
        if err:
            return err

        # Determine the bare API path to call
        parsed = urlparse(href)
        if parsed.netloc:
            # Full URL: strip to the API path segment
            api_path = _strip_to_api_path(href)
        else:
            # Bare path: strip the versioned prefix if present, else use as-is
            api_path = href.split("?")[0]
            if api_path.startswith(_API_PATH_SEGMENT):
                api_path = api_path[len(_API_PATH_SEGMENT):]

        if not api_path.startswith("/"):
            api_path = "/" + api_path

        # Sanitize query params from original href (strip unknown params)
        safe_params: dict[str, Any] = _sanitize_query_params(href)

        # Apply caller-supplied fields / pagination (override href params)
        safe_params["page"] = page
        safe_params["pageSize"] = min(page_size, config.MAX_PAGE_SIZE)

        if fields:
            safe_params["fields"] = ",".join(fields)
        elif "fields" not in safe_params and response_format == "concise":
            # Apply default projection from registry if we can identify the entity
            entity = _map_to_entity(api_path)
            if entity:
                from cwpsa.registry.loader import get_registry
                record = get_registry().get_entity(entity)
                if record and record.default_projection:
                    safe_params["fields"] = ",".join(record.default_projection)

        # Execute (GET only — enforced by cw_get wrapper)
        from cwpsa.integration.client import cw_get as _cw_get
        from cwpsa.links import attach_links, extract_links
        try:
            data = await _cw_get(api_path, **safe_params)
        except Exception as exc:
            return upstream_error(str(exc))

        # Response governance envelope (mirrors cw_query)
        if isinstance(data, list):
            has_more = len(data) >= page_size
            links = extract_links(data[0] if data else {}, config.CW_BASE_URL, response_format)
            result: dict[str, Any] = {
                "href_path": api_path,
                "count_hint": len(data),
                "data": data,
                "has_more": has_more,
                "next_cursor": page + 1 if has_more else None,
                "message": (
                    f"showing {len(data)} records -- page for more"
                    if has_more
                    else f"showing all {len(data)} records"
                ),
            }
            if links:
                result["_links"] = links
            return result

        # Single record — attach _links + _version
        if isinstance(data, dict):
            if "_info" in data:
                data["_version"] = data["_info"].get("lastUpdated")
            attach_links(data, config.CW_BASE_URL, response_format)
        return data
