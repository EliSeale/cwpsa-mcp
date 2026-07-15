"""
ConnectWise PSA MCP Server — advanced registry-driven implementation.

Architecture: see connectwise_mcp_architecture.md
Legacy server: see legacy/Server.py

Startup sequence:
  1. Load config (Key Vault secrets).
  2. Initialize OTel tracing + metrics.
  3. Load registry artifact.
  4. Build Entra auth provider (or None for unauthenticated stdio).
  5. Warm reference cache (boards, priorities, etc.) → inject into instructions.
  6. Register Tier 1 + Tier 2 tools, Resources, and Prompts on FastMCP.
  7. Run: stdio (local/Claude Desktop) or Streamable HTTP (Azure Container Apps).

Run modes:
  python -m cwpsa.server               # stdio (local)
  MCP_TRANSPORT=http python -m cwpsa.server   # Streamable HTTP
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server instructions (static base + live vocabulary injected at startup)
# ---------------------------------------------------------------------------

_INSTRUCTIONS_BASE = """\
ConnectWise PSA MCP server (registry-driven, v2).

TIER 1 GENERIC TOOLS -- cover all 283 CW entities parametrically:
  cw_describe(entity)            -> field manifest (types, filterability, enums, projection)
  cw_query(entity, filter)       -> filtered paginated list (structured DSL -- no raw conditions)
  cw_get(entity, id)             -> single authoritative record + _version for concurrency
  cw_count(entity, filter)       -> cheap count before a large query
  cw_resolve(reference_type, q)  -> fuzzy name/alias -> CW IDs and exact values
  cw_follow_href(href)           -> follow a ConnectWise _info href safely (validated GET only)
  cw_create / cw_update / cw_delete -> mutations (write-gated; confirm before delete)

TIER 2 WORKFLOW TOOLS -- pre-wire common MSP intents with automatic resolution:
  cw_list_tickets / cw_get_ticket / cw_create_ticket / cw_update_ticket
  cw_find_company / cw_list_contacts
  cw_log_time
  cw_list_invoices / cw_get_agreement
  cw_list_configurations
  cw_list_opportunities

FILTER DSL -- the agent never writes raw ConnectWise conditions strings.
Use cw_query's `filter` parameter with a structured object:
  {
    "conditions":   {"and": [{"field":"status/name","op":"=","value":"Open"}, ...]},
    "order_by":     [{"field":"lastUpdated","dir":"desc"}],
    "fields":       ["id","summary","company/name","status/name"],
    "page":         1,
    "page_size":    25
  }
  Operators: = != < <= > >= contains not_contains like not_like in not_in
  Values: strings auto-quoted; datetimes as UTC ISO-8601 "2026-06-01T00:00:00Z";
          booleans as true/false (JSON); null for null.

RESOLUTION WORKFLOW:
  Before filtering by company/member/board/status/priority name, always resolve first:
  cw_resolve("company", "ACME")                -> [{id, identifier, name}]
  cw_resolve("status", "New", {"board":"..."}) -> [{id, name}]  (status is board-scoped)
  cw_resolve("member", "John")                 -> [{id, name}]
  If multiple matches are returned, ask the user which one they meant.

HREF FOLLOWING (_info links):
  ConnectWise responses include an _info object with *_href keys (teams_href, sites_href,
  contacts_href, tickets_href, notes_href, etc.). Use cw_follow_href to fetch those resources:
    cw_follow_href(href)  -- validates host + path, strips unknown params, applies governance.
  Example workflow for account manager lookup:
    1. cw_get("company/companies", 123)          -> _info.teams_href
    2. cw_follow_href(_info.teams_href)           -> team members with roles
  Always prefer cw_get / cw_query for primary navigation. cw_follow_href is for hrefs
  that are not reachable any other way.

STANDING FILTERS -- always apply unless the user explicitly overrides:
  - Companies: always include deletedFlag=false (or use cw_find_company / cw_list_contacts)

PAGINATION:
  cw_query returns has_more=true + next_cursor when there are more records.
  Use cw_count first for "how many" questions.

WRITES:
  Resolve IDs before mutating -- never mutate based on fuzzy names.
  cw_delete requires confirm=true -- always confirm destructive intent with the user.
  All mutations respect the CW_WRITES_DISABLED kill-switch.
"""


# ---------------------------------------------------------------------------
# Build the FastMCP server
# ---------------------------------------------------------------------------

def create_server() -> FastMCP:
    """Build and configure the FastMCP MCP server."""
    from cwpsa.auth.entra import build_auth_provider
    from cwpsa.auth.pep import PEPMiddleware
    from cwpsa.integration.client import get_circuit_state
    from cwpsa.observability.tracing import setup_tracing
    from cwpsa.observability.metrics import setup_metrics
    from cwpsa.registry.loader import get_registry

    # 1. Observability
    setup_tracing()
    setup_metrics()

    # 2. Registry (pre-load to catch missing artifact early)
    registry = get_registry()
    log.info("Registry loaded: %d entities.", len(registry.entities))

    # 3. Auth provider
    auth_provider = build_auth_provider()

    # 4. Build instructions (vocabulary injected below after server is created)
    instructions = _INSTRUCTIONS_BASE

    # 5. FastMCP server
    mcp = FastMCP(
        name="ConnectWise PSA",
        auth=auth_provider,
        instructions=instructions,
    )

    # 6. PEP middleware — audit logging + kill-switch enforcement
    mcp.add_middleware(PEPMiddleware())

    # 7. Health endpoint (§12.4)
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Health probe: confirms registry loaded + circuit state.

        Used by Container Apps liveness/readiness probes and Docker HEALTHCHECK.
        Returns 200 when healthy, 503 when circuit is open (CW unreachable).
        """
        circuit = get_circuit_state()
        entity_count = len(registry.entities)
        healthy = circuit != "open" and entity_count > 0
        payload = {
            "status": "ok" if healthy else "degraded",
            "registry_entities": entity_count,
            "circuit": circuit,
        }
        return JSONResponse(payload, status_code=200 if healthy else 503)

    # 8. Register tools
    _register_tools(mcp)

    # 9. Register Resources + Prompts
    from cwpsa import resources, prompts
    resources.register(mcp)
    prompts.register(mcp)

    return mcp


def _register_tools(mcp: FastMCP) -> None:
    """Register all Tier 1 and Tier 2 tools on the MCP server."""
    # Tier 1 — always loaded (critical subset, §4.7)
    from cwpsa.tools.tier1 import describe, query, get, count, resolve, follow_href
    describe.register(mcp)
    query.register(mcp)
    get.register(mcp)
    count.register(mcp)
    resolve.register(mcp)
    follow_href.register(mcp)

    # Tier 1 — mutations (deferrable; loaded but annotated destructive/write)
    from cwpsa.tools.tier1 import mutate
    mutate.register(mcp)

    # Tier 2 — workflow tools (deferrable, §4.7)
    from cwpsa.tools.tier2 import tickets, companies, time, finance, configurations, sales
    tickets.register(mcp)
    companies.register(mcp)
    time.register(mcp)
    finance.register(mcp)
    configurations.register(mcp)
    sales.register(mcp)


# ---------------------------------------------------------------------------
# Vocabulary injection (live CW reference data → instructions, §5)
# ---------------------------------------------------------------------------

async def _inject_vocabulary(mcp: FastMCP) -> None:
    """Fetch reference data and append live vocabulary to server instructions."""
    from cwpsa import config
    if not config.LOAD_VOCABULARY:
        log.info("[vocab] skipped (CW_LOAD_VOCABULARY=0)")
        return

    try:
        from cwpsa.resolution.cache import warm_cache
        vocab = await warm_cache()
        if vocab:
            extra = (
                "\n\nLIVE VOCABULARY in this ConnectWise instance "
                "(use these exact values; board statuses are board-scoped — "
                "call cw_resolve('status', ..., {'board': '...'}) for them):\n"
                + vocab
            )
            mcp.instructions = (mcp.instructions or "") + extra
            log.info("[vocab] live vocabulary injected into server instructions.")
    except Exception as exc:
        log.warning("[vocab] reference fetch failed, skipping vocabulary injection: %s", exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

_mcp: FastMCP | None = None


def get_mcp() -> FastMCP:
    """Return the server singleton (lazy-initialized)."""
    global _mcp
    if _mcp is None:
        _mcp = create_server()
    return _mcp


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from cwpsa import config

    mcp = get_mcp()

    transport = os.getenv("MCP_TRANSPORT")

    # Warm vocabulary before serving
    asyncio.run(_inject_vocabulary(mcp))

    auth_status = "ENABLED" if config.ENTRA_TENANT_ID else "DISABLED (unauthenticated)"
    log.info("[auth] Entra ID: %s", auth_status)

    if transport == "http" and not config.ENTRA_TENANT_ID:
        log.warning(
            "[auth] WARNING: serving HTTP with no authentication. "
            "Set ENTRA_TENANT_ID and ENTRA_CLIENT_ID before exposing this endpoint."
        )

    if transport == "http":
        mcp.run(
            transport="http",
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("MCP_PORT", "8080")),
            stateless_http=True,
        )
    else:
        mcp.run()
