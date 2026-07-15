"""
ConnectWise PSA MCP server — generated from the pruned OpenAPI spec via FastMCP.

    pip install fastmcp httpx azure-identity azure-keyvault-secrets python-dotenv
    python server.py                   # stdio (local / Claude Desktop)
    MCP_TRANSPORT=http python server.py   # Streamable HTTP (Azure Container Apps)

Two independent auth layers:
  - ConnectWise client auth (Layer 1): how THIS server talks to the PSA. Basic
    auth with the member key below. Currently one shared credential — restrict
    it to a read-only ConnectWise security role for the leadership pilot.
  - Entra ID auth (Layer 2): who is allowed to call THIS server. Validates
    incoming Entra JWTs. Requires ENTRA_TENANT_ID and ENTRA_CLIENT_ID secrets
    in Key Vault.

Secrets are pulled from Azure Key Vault at startup using DefaultAzureCredential
(managed identity in production, az login / VS Code credential locally).
Set the KEY_VAULT_URL environment variable to your vault, e.g.:
    KEY_VAULT_URL=https://my-vault.vault.azure.net/

On startup the server also fetches small ConnectWise reference sets (boards,
priorities, etc.) and injects their exact values into the server instructions,
so the model translates user language to real system values. Disable with
CW_LOAD_VOCABULARY=0 (e.g. for fast local stdio launches).
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()  # loads .env into os.environ (no-op if file absent)
from azure.keyvault.secrets import SecretClient
from fastmcp import FastMCP

from business_knowledge import load_okf_bundle, instructions_block, register_business_knowledge

_bundle = load_okf_bundle(os.getenv("OKF_BUNDLE_PATH", "business-knowledge"))

# --- Key Vault client -------------------------------------------------------
_vault_url = os.environ["KEY_VAULT_URL"]
_credential = DefaultAzureCredential()
_kv = SecretClient(vault_url=_vault_url, credential=_credential)

def _secret(name: str, default: str | None = None) -> str:
    """Fetch a secret from Key Vault, falling back to default if not found."""
    try:
        return _kv.get_secret(name).value
    except Exception:
        if default is not None:
            return default
        raise

# --- config from Key Vault --------------------------------------------------
SPEC_PATH = os.getenv("CW_SPEC_PATH", "openapi-spec-pruned.json")

CW_URL = "https://connect.verveit.com"
if "/v4_6_release/apis/3.0" not in CW_URL:
    CW_URL += "/v4_6_release/apis/3.0"

CW_COMPANY     = "mettle"
CW_PUBLIC      = _secret("cw-publickey-01-mcp")
CW_PRIVATE     = _secret("cw-privatekey-01-mcp")
CW_CLIENT_ID   = _secret("cw-clientid-01-mcp")
CW_API_VERSION = _secret("cw-apiversion-01-mcp", "2022.1")

# Shared auth + headers, reused by both the async client (request handling) and
# the short-lived sync client used for the one-time startup vocabulary fetch.
CW_AUTH = httpx.BasicAuth(f"{CW_COMPANY}+{CW_PUBLIC}", CW_PRIVATE)
CW_HEADERS = {
    "clientId": CW_CLIENT_ID,
    "Accept": f"application/vnd.connectwise.com+json; version={CW_API_VERSION}",
    "Content-Type": "application/json",
}

# --- ConnectWise HTTP client (Layer 1: Basic auth + clientId + version) ------
# Auth username = "<companyId>+<publicKey>", password = "<privateKey>".
cw_client = httpx.AsyncClient(base_url=CW_URL, auth=CW_AUTH, headers=CW_HEADERS, timeout=30.0)

# --- Entra ID authentication (Layer 2: caller -> this server) ---------------
ENTRA_TENANT_ID = _secret("entra-tenantid-01-mcp")
ENTRA_CLIENT_ID = _secret("entra-clientid-01-mcp")

ENTRA_REQUIRED_SCOPES = (
    _secret("entra-requiredscopes-01-mcp").replace(",", " ").split() or None
)

auth_provider = None
if ENTRA_TENANT_ID and ENTRA_CLIENT_ID:
    from fastmcp.server.auth.providers.azure import AzureJWTVerifier

    auth_provider = AzureJWTVerifier(
        client_id=ENTRA_CLIENT_ID,
        tenant_id=ENTRA_TENANT_ID,
        required_scopes=ENTRA_REQUIRED_SCOPES,
    )

# --- startup vocabulary fetch (enums) ---------------------------------------
# Pull small, stable reference sets and inject their exact values into the
# instructions so the model uses real system vocabulary. A short-lived SYNC
# client is used on purpose: it avoids binding the async cw_client to a
# throwaway event loop at import time. This never fails startup.
REFERENCE_SETS = {
    "Service boards": "/service/boards",
    "Ticket priorities": "/service/priorities",
    "SLAs": "/service/SLAs",
    "Company statuses": "/company/companies/statuses",
    "Company types": "/company/companies/types",
}

def _bootstrap_vocabulary() -> str:
    lines: list[str] = []
    try:
        with httpx.Client(base_url=CW_URL, auth=CW_AUTH, headers=CW_HEADERS, timeout=15.0) as c:
            for label, path in REFERENCE_SETS.items():
                try:
                    resp = c.get(path, params={"fields": "id,name", "pageSize": 200})
                    resp.raise_for_status()
                    names = [row["name"] for row in resp.json() if row.get("name")]
                    if names:
                        lines.append(f"- {label}: {', '.join(names)}")
                except Exception as exc:
                    print(f"[vocab] skipped {label}: {exc}")
    except Exception as exc:
        print(f"[vocab] reference fetch unavailable: {exc}")
    return "\n".join(lines)

INSTRUCTIONS_BASE = """\
ConnectWise PSA tools (read-biased pilot). Most tools wrap the PSA REST API 1:1.

BUSINESS CONTEXT — When a user requests information, use the get_business_concept tool first to find any relevant metrics, entities, or playbooks.
This lets you know the business context of what the user is requesting.

STANDING FILTERS — always apply these unless the user explicitly asks otherwise:
  - Companies:   always include deletedFlag=false in conditions (deleted companies must never appear)

TRANSLATING USER LANGUAGE TO SYSTEM VALUES
Users speak in names ("ACME", "high priority", "the Help Desk board", "tickets owned
by John"). Before building a `conditions` filter, resolve those names to exact system
values with the resolver tools — do NOT guess ids or status spellings:
  - resolve_company(query)               -> [{id, identifier, name}] (filter: company/identifier="MWH" or company/id=123)
  - resolve_priority(name)               -> exact priority name + id (filter: priority/name="...")
  - resolve_board(name)                  -> board id + name
  - resolve_board_status(board, status)  -> exact status name        (statuses are per-board)
  - resolve_member(query)                -> [{id, name}]             (filter: owner/id=7)
Company queries may be an acronym/abbreviation (matched against identifier) or part of the
name; member queries may be just a first name, last name, full name, or login id.
If a resolver returns more than one match, ask the user which one they meant.

FOLLOWING HREF LINKS (HATEOAS)
Every ConnectWise response contains an `_info` object with `*_href` values pointing to
related resources (e.g. `sites_href`, `teams_href`, `contacts_href`, `tickets_href`,
`opportunities_href`). Use the `lookup` tool to follow any of these — pass the href
value directly and it will return that resource.

Workflow example — "who is the account manager for ACME?":
  1. resolve_company("ACME")                     -> {id: 42, ...}
  2. lookup("/company/companies/42")             -> full record including _info.teams_href
  3. lookup(_info.teams_href)                    -> team members with roles (account manager is a team role)

Use `lookup` any time you need information that isn't directly in the initial response
but is reachable via an href. Prefer this over guessing paths or making assumptions.

BUILDING LIST QUERIES
Use the `conditions` parameter with ConnectWise syntax, e.g.
  conditions="company/identifier=\\"ACME\\" and status/name=\\"New\\"", orderBy="dateEntered desc"
Always set a small pageSize; never request large pages.

"""

if os.getenv("CW_LOAD_VOCABULARY", "1") != "0":
    _vocab = _bootstrap_vocabulary()
else:
    _vocab = ""
    print("[vocab] skipped (CW_LOAD_VOCABULARY=0)")

INSTRUCTIONS = INSTRUCTIONS_BASE + instructions_block(_bundle)   # injects the always-in-context concept index
if _vocab:
    INSTRUCTIONS += (
        "\n\nLIVE VOCABULARY in this ConnectWise instance (use these exact values; "
        "board statuses are fetched per-board via resolve_board_status):\n" + _vocab
    )
    print(f"[vocab] loaded {_vocab.count(chr(10)) + 1} reference set(s) into instructions")

# --- generate the MCP server from the pruned spec ---------------------------
openapi_spec = json.loads(Path(SPEC_PATH).read_text(encoding="utf-8"))

mcp = FastMCP.from_openapi(
    openapi_spec=openapi_spec,
    client=cw_client,
    name="ConnectWise PSA",
    auth=auth_provider,        # None = unauthenticated (local stdio); set ENTRA_* to protect HTTP
    instructions=INSTRUCTIONS, # workflow + translation guidance + live vocabulary
)

# Register business knowledge tool (get_business_concept)
register_business_knowledge(mcp, _bundle)

# --- resolver helpers -------------------------------------------------------
async def _cw_get(path: str, **params) -> list[dict]:
    """GET a ConnectWise endpoint and return the parsed JSON list."""
    resp = await cw_client.get(path, params=params)
    resp.raise_for_status()
    return resp.json()

def _match(rows: list[dict], query: str, key: str = "name", limit: int = 10) -> list[dict]:
    """Pick rows whose `key` matches `query`: exact -> substring -> fuzzy."""
    q = (query or "").strip().lower()
    if not q:
        return rows[:limit]
    exact = [r for r in rows if str(r.get(key, "")).lower() == q]
    if exact:
        return exact
    sub = [r for r in rows if q in str(r.get(key, "")).lower()]
    if sub:
        return sub[:limit]
    close = set(difflib.get_close_matches(q, [str(r.get(key, "")).lower() for r in rows],
                                          n=limit, cutoff=0.6))
    return [r for r in rows if str(r.get(key, "")).lower() in close]

def _norm(s: str) -> str:
    """Lowercase, alphanumeric-only — so 'McDonald's' and 'McDonalds' compare equal."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

# --- resolver tools (name -> system id/value) -------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def resolve_company(query: str) -> list[dict]:
    """Resolve a company to {id, identifier, name}. `query` may be an acronym or
    abbreviation (matched against the identifier, e.g. "MWH") or part of the name
    (e.g. "McDonalds"). Searches name AND identifier, then ranks with fuzzy matching
    that ignores punctuation/case. Use company/identifier="..." or company/id=... in
    conditions. Returns up to 10 ranked matches; if more than one, confirm with the user."""
    safe = query.replace('"', "").strip()
    rows = await _cw_get("/company/companies",
                         conditions=f'deletedFlag!=true and (name contains "{safe}" or identifier contains "{safe}")',
                         fields="id,identifier,name", pageSize=25)
    # Fallback: punctuation/plural mismatches (server `contains` is literal). Retry on a
    # shorter token, e.g. "McDonalds" -> "McDonald" still matches "McDonald's of ...".
    if not rows and safe.split():
        token = re.sub(r"s$", "", safe.split()[0])
        if token and token.lower() != safe.lower():
            rows = await _cw_get("/company/companies",
                                 conditions=f'deletedFlag!=true and (name contains "{token}" or identifier contains "{token}")',
                                 fields="id,identifier,name", pageSize=25)
    nq = _norm(query)

    def _score(r: dict) -> float:
        ident, name = _norm(r.get("identifier", "")), _norm(r.get("name", ""))
        if nq and nq == ident:
            return 1.0
        cands = [c for c in (ident, name) if c]
        base = 0.9 if any(nq and (nq in c or c in nq) for c in cands) else 0.0
        fuzzy = max((difflib.SequenceMatcher(None, nq, c).ratio() for c in cands), default=0.0)
        return max(base, fuzzy)

    return sorted(rows, key=_score, reverse=True)[:10]

@mcp.tool(annotations={"readOnlyHint": True})
async def resolve_priority(name: str) -> list[dict]:
    """Resolve a priority phrase (e.g. 'high', 'emergency') to the exact ConnectWise
    priority name + id. Use the exact name in conditions: priority/name="...". """
    return _match(await _cw_get("/service/priorities", fields="id,name", pageSize=100), name)

@mcp.tool(annotations={"readOnlyHint": True})
async def resolve_board(name: str) -> list[dict]:
    """Resolve a service board name to its id + exact name. Call this before
    resolving board-specific statuses or types."""
    return _match(await _cw_get("/service/boards", fields="id,name", pageSize=200), name)

@mcp.tool(annotations={"readOnlyHint": True})
async def resolve_board_status(board: str, status: str) -> dict | list[dict]:
    """Resolve a status name within a service board (statuses are board-specific).
    Returns matching statuses with id + exact name; use status/name="..." in conditions.
    If the board name is ambiguous, returns the candidate boards to disambiguate."""
    boards = _match(await _cw_get("/service/boards", fields="id,name", pageSize=200), board)
    if not boards:
        return {"error": f"No service board matches '{board}'."}
    if len(boards) > 1:
        return {"error": "Multiple boards match; ask the user which one.",
                "boards": boards}
    bid = boards[0]["id"]
    statuses = await _cw_get(f"/service/boards/{bid}/statuses", fields="id,name", pageSize=200)
    return _match(statuses, status)

@mcp.tool(annotations={"readOnlyHint": True})
async def resolve_member(query: str) -> list[dict]:
    """Resolve a member to {id, name}. `query` may be just a first name, last name,
    full name, or login id (e.g. 'John', 'Smith', 'John Smith', 'jsmith'). Use the
    returned `id` in conditions, e.g. owner/id=7. Returns up to 10 ranked matches;
    if more than one, confirm with the user which person they meant."""
    rows = await _cw_get("/system/members",
                         fields="id,identifier,firstName,lastName", pageSize=1000)
    q = query.strip().lower()
    scored: list[tuple[float, dict]] = []
    for m in rows:
        first = (m.get("firstName") or "").lower()
        last = (m.get("lastName") or "").lower()
        full = f"{first} {last}".strip()
        ident = (m.get("identifier") or "").lower()
        if not q:
            continue
        if q in {first, last, ident}:                                   # exact first / last / login
            s = 1.0
        elif q in full or q in ident or first.startswith(q) or last.startswith(q):
            s = 0.85
        else:                                                           # fuzzy fallback
            s = max((difflib.SequenceMatcher(None, q, h).ratio()
                     for h in (first, last, full, ident) if h), default=0.0)
        if s >= 0.6:
            name = f"{m.get('firstName', '')} {m.get('lastName', '')}".strip()
            scored.append((s, {"id": m["id"], "name": name}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:10]]

# --- generic href lookup tool -----------------------------------------------
_CW_BASE = CW_URL

@mcp.tool(annotations={"readOnlyHint": True})
async def lookup(
    href: str,
    conditions: str = "",
    child_conditions: str = "",
    custom_field_conditions: str = "",
    order_by: str = "",
    fields: str = "",
    page: int = 1,
    page_size: int = 25,
) -> object:
    """Follow any ConnectWise API href or path to retrieve its data.

    ConnectWise responses include an `_info` object with `*_href` values such as
    `teams_href`, `sites_href`, `contacts_href`, `tickets_href`, etc. Pass the
    value of any such href directly into this tool to retrieve that resource.

    Also accepts bare paths like "/company/companies/123/teams".

    Parameters mirror the standard ConnectWise API query parameters:
      conditions            — filter on top-level fields (e.g. 'status/name="New"')
      child_conditions      — filter inside nested arrays (e.g. 'communications/type="Email"')
      custom_field_conditions — filter on custom fields (e.g. 'caption="Priority" AND value="High"')
      order_by              — sort results (e.g. 'dateEntered desc')
      fields                — comma-separated fields to return (e.g. 'id,name,status/name')
      page                  — 1-based page number for pagination
      page_size             — records per page (default 25, max 1000)

    Examples:
      lookup("/company/companies/42")
      lookup("/company/companies/42/teams")
      lookup("/service/tickets", conditions='company/id=42 and closedFlag=false', order_by="dateEntered desc")
      lookup("/company/companies/42/contacts", fields="id,firstName,lastName,defaultFlag")
    """
    path = href.strip()
    for prefix in (_CW_BASE, _CW_BASE.rstrip("/")):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if "?" in path:
        path = path.split("?", 1)[0]

    params: dict = {"pageSize": page_size, "page": page}
    if conditions:
        params["conditions"] = conditions
    if child_conditions:
        params["childConditions"] = child_conditions
    if custom_field_conditions:
        params["customFieldConditions"] = custom_field_conditions
    if order_by:
        params["orderBy"] = order_by
    if fields:
        params["fields"] = fields

    resp = await cw_client.get(path, params=params)
    resp.raise_for_status()
    return resp.json()


# --- count tool -------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def count_records(
    path: str,
    conditions: str = "",
    child_conditions: str = "",
    custom_field_conditions: str = "",
) -> dict:
    """Return the total number of records for any ConnectWise list endpoint.

    Appends /count to the given path and returns {"count": <int>}.

      conditions            — filter on top-level fields (e.g. 'status/name="New"')
      child_conditions      — filter inside nested arrays (e.g. 'communications/type="Email"')
      custom_field_conditions — filter on custom fields

    Examples:
      count_records("/service/tickets", conditions='status/name="New"')
      count_records("/company/companies")
      count_records("/finance/agreements", conditions='cancelledFlag=false')

    Use this whenever you need to know how many records exist before deciding
    whether to paginate, or to answer a "how many..." question directly.
    The path should start with / and must be a list endpoint (e.g. /service/tickets,
    /project/projects, /company/contacts). Do not include /count yourself.
    """
    count_path = path.rstrip("/") + "/count"
    params = {}
    if conditions:
        params["conditions"] = conditions
    if child_conditions:
        params["childConditions"] = child_conditions
    if custom_field_conditions:
        params["customFieldConditions"] = custom_field_conditions
    resp = await cw_client.get(count_path, params=params)
    resp.raise_for_status()
    return {"count": resp.json()}


# --- hand-authored tools that aren't in the OpenAPI spec --------------------
# Your Azure AI Search hybrid-retrieval tool attaches here, on the same server:
#
# @mcp.tool(annotations={"readOnlyHint": True})
# async def search_tickets_semantic(
#     query: str, company_id: int | None = None, top_k: int = 8
# ) -> list[dict]:
#     """Hybrid (vector + keyword) search over ticket notes. Returns ranked
#     candidates with ticketId + provenance for the agent to hydrate live via
#     get_service_ticket. Does NOT return full ticket bodies."""
#     ...

# --- run --------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT")
    print(f"[auth] Entra ID: {'ENABLED' if auth_provider else 'DISABLED (unauthenticated)'}")
    if transport == "http" and auth_provider is None:
        print(
            "[auth] WARNING: serving HTTP with no authentication. Set ENTRA_TENANT_ID "
            "and ENTRA_CLIENT_ID before exposing this endpoint."
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