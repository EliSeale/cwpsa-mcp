"""
Local integration test script -- runs against the live ConnectWise instance.

Usage:
    python scripts/test_local.py               # all tests
    python scripts/test_local.py describe      # registry tests only (no network)
    python scripts/test_local.py live          # live CW API tests

Requires:
    - KEY_VAULT_URI (or KEY_VAULT_URL) in .env + az login / VS Code credential, OR
    - CW_LOCAL_SECRETS=1 with CWPSA_COMPANY_ID, CWPSA_CLIENT_ID,
      CWPSA_INTEGRATOR_USERNAME, CWPSA_INTEGRATOR_PASSWORD in .env

Per-user keys are minted at runtime by the token broker (§10.6).
No static API member public/private keys are needed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

from fastmcp import Client
from cwpsa.server import create_server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _h(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def _ok(label: str, detail: str = "") -> None:
    print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))


def _fail(label: str, err: Any) -> None:
    print(f"  [FAIL] {label}: {err}")


async def _call(client: Client, tool: str, args: dict) -> Any:
    """Call a tool and return its .data (dict/list). Raises on MCP error."""
    result = await client.call_tool(tool, args)
    if result.is_error:
        raise RuntimeError(f"{tool}: {result.content}")
    return result.data


# ---------------------------------------------------------------------------
# Registry tests (pure -- no ConnectWise network calls)
# ---------------------------------------------------------------------------

async def test_registry(client: Client) -> int:
    _h("Registry tests (no network)")
    failures = 0

    # 1. List all entities
    try:
        data = await _call(client, "cw_describe", {"entity": "?"})
        entities = data.get("entities", [])
        assert len(entities) > 200, f"Expected >200 entities, got {len(entities)}"
        _ok(f"Entity catalog", f"{len(entities)} entities")
    except Exception as e:
        _fail("Entity catalog", e); failures += 1

    # 2. Describe service/tickets
    try:
        data = await _call(client, "cw_describe", {"entity": "service/tickets"})
        assert "fields" in data and "default_projection" in data
        assert "id" in data["default_projection"]
        assert "query" in data.get("operations", [])
        _ok("cw_describe(service/tickets)",
            f"{len(data['fields'])} fields, projection={data['default_projection'][:4]}")
    except Exception as e:
        _fail("cw_describe(service/tickets)", e); failures += 1

    # 3. Alias resolution -- 'ticket' should resolve to service/tickets
    try:
        data = await _call(client, "cw_describe", {"entity": "ticket"})
        assert data.get("entity") == "service/tickets", f"Got {data.get('entity')}"
        _ok("Alias 'ticket' -> service/tickets")
    except Exception as e:
        _fail("Alias resolution", e); failures += 1

    # 4. Describe company/companies
    try:
        data = await _call(client, "cw_describe", {"entity": "company/companies"})
        _ok("cw_describe(company/companies)", f"{len(data['fields'])} fields, ops={data['operations']}")
    except Exception as e:
        _fail("cw_describe(company/companies)", e); failures += 1

    # 5. full=True returns more fields than lean default
    try:
        lean = await _call(client, "cw_describe", {"entity": "service/tickets"})
        full = await _call(client, "cw_describe", {"entity": "service/tickets", "full": True})
        assert len(full["fields"]) >= len(lean["fields"])
        _ok("cw_describe(full=True)", f"lean={len(lean['fields'])} fields, full={len(full['fields'])} fields")
    except Exception as e:
        _fail("cw_describe(full=True)", e); failures += 1

    # 6. Validation error -- unknown field
    try:
        data = await _call(client, "cw_count", {
            "entity": "service/tickets",
            "filter": {"conditions": {"field": "nonExistentXYZField", "op": "=", "value": "x"}}
        })
        assert "error" in data, f"Expected error envelope, got: {data}"
        assert data["error"]["code"] == "validation_error"
        msg = data["error"]["message"]
        suggestions = data["error"].get("details", {}).get("suggestions", [])
        _ok("Validation error for unknown field", f"'{msg[:60]}' suggestions={suggestions}")
    except Exception as e:
        _fail("Validation error handling", e); failures += 1

    return failures


# ---------------------------------------------------------------------------
# Live ConnectWise tests
# ---------------------------------------------------------------------------

async def test_live(client: Client) -> int:
    _h("Live ConnectWise tests (requires CW credentials)")
    failures = 0

    # 7. Resolve company
    try:
        data = await _call(client, "cw_resolve", {"reference_type": "company", "query": "Verve"})
        assert isinstance(data, list) and len(data) > 0
        _ok("cw_resolve('company', 'Verve')", f"{len(data)} match(es): {[c.get('name') for c in data[:3]]}")
    except Exception as e:
        _fail("cw_resolve company", e); failures += 1

    # 8. Resolve member
    try:
        data = await _call(client, "cw_resolve", {"reference_type": "member", "query": "Eli"})
        assert isinstance(data, list)
        _ok("cw_resolve('member', 'Eli')", f"{len(data)} match(es): {[m.get('name') for m in data[:3]]}")
    except Exception as e:
        _fail("cw_resolve member", e); failures += 1

    # 9. Resolve board
    try:
        data = await _call(client, "cw_resolve", {"reference_type": "board", "query": "Service"})
        assert isinstance(data, list)
        _ok("cw_resolve('board', 'Service')", f"{len(data)} match(es): {[b.get('name') for b in data[:3]]}")
    except Exception as e:
        _fail("cw_resolve board", e); failures += 1

    # 10. Count open tickets
    try:
        data = await _call(client, "cw_count", {
            "entity": "service/tickets",
            "filter": {"conditions": {"field": "closedFlag", "op": "=", "value": False}}
        })
        assert "count" in data
        _ok("cw_count(open tickets)", f"count={data['count']}")
    except Exception as e:
        _fail("cw_count open tickets", e); failures += 1

    # 11. List open tickets
    try:
        t0 = time.monotonic()
        data = await _call(client, "cw_list_tickets", {"closed": False, "page_size": 5})
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert "data" in data
        _ok("cw_list_tickets(closed=False, page_size=5)",
            f"{data['count_hint']} returned, has_more={data['has_more']}, {elapsed_ms:.0f}ms")
        if data["data"]:
            t = data["data"][0]
            print(f"         First ticket: #{t.get('id')} "
                  f"'{t.get('summary','?')[:50]}' "
                  f"status={t.get('status',{}).get('name','?')}")
    except Exception as e:
        _fail("cw_list_tickets", e); failures += 1

    # 12. cw_query with DSL filter
    try:
        data = await _call(client, "cw_query", {
            "entity": "service/tickets",
            "filter": {
                "conditions": {"field": "closedFlag", "op": "=", "value": False},
                "order_by": [{"field": "requiredDate", "dir": "desc"}],
                "fields": ["id", "summary", "status", "company", "requiredDate"],
                "page_size": 3
            }
        })
        _ok("cw_query(tickets, DSL filter)", f"{data['count_hint']} records, has_more={data['has_more']}")
    except Exception as e:
        _fail("cw_query DSL filter", e); failures += 1

    # 13. cw_find_company
    try:
        data = await _call(client, "cw_find_company", {"name": "Verve"})
        if "candidates" in data:
            _ok("cw_find_company('Verve')",
                f"multiple matches: {[c.get('name') for c in data['candidates'][:3]]}")
        else:
            _ok("cw_find_company('Verve')", f"name={data.get('name','?')}, id={data.get('id','?')}")
    except Exception as e:
        _fail("cw_find_company", e); failures += 1

    # 14. cw_query with IN operator
    try:
        data = await _call(client, "cw_query", {
            "entity": "service/tickets",
            "filter": {
                "conditions": {
                    "and": [
                        {"field": "closedFlag", "op": "=", "value": False},
                        {"field": "billTime", "op": "in", "value": ["Billable", "NoCharge"]}
                    ]
                },
                "fields": ["id", "summary", "billTime"],
                "page_size": 3
            }
        })
        _ok("cw_query(tickets, IN operator)", f"{data['count_hint']} records")
    except Exception as e:
        _fail("cw_query IN operator", e); failures += 1

    # 15. cw_list_tickets filtered by board name
    try:
        # First resolve a board name
        boards = await _call(client, "cw_resolve", {"reference_type": "board", "query": "Service"})
        if boards:
            board_name = boards[0]["name"]
            data = await _call(client, "cw_list_tickets", {
                "board": board_name, "closed": False, "page_size": 3
            })
            _ok(f"cw_list_tickets(board='{board_name}')",
                f"{data['count_hint']} tickets returned")
        else:
            _ok("cw_list_tickets(board) skipped", "no boards found")
    except Exception as e:
        _fail("cw_list_tickets filtered by board", e); failures += 1

    # 16. cw_follow_href -- follow an _info href from a company record
    try:
        # Find a company first
        companies = await _call(client, "cw_resolve", {"reference_type": "company", "query": "Verve"})
        if companies:
            company_id = companies[0]["id"]
            company = await _call(client, "cw_get", {"entity": "company/companies", "id": company_id})
            # Build a teams href manually (same as what _info.teams_href would contain)
            from cwpsa import config as cw_config
            base = cw_config.CW_BASE_URL
            teams_href = f"{base}/company/companies/{company_id}/teams"
            data = await _call(client, "cw_follow_href", {"href": teams_href, "page_size": 10})
            if isinstance(data, dict) and "data" in data:
                _ok("cw_follow_href(teams_href)",
                    f"{data['count_hint']} team members for company {company_id}")
            else:
                _ok("cw_follow_href(teams_href)", f"returned: {type(data).__name__}")
        else:
            _ok("cw_follow_href skipped", "no Verve company found")
    except Exception as e:
        _fail("cw_follow_href", e); failures += 1

    # 17. cw_follow_href -- validation rejects external URL
    try:
        data = await _call(client, "cw_follow_href", {"href": "https://evil.com/steal/data"})
        assert "error" in data, f"Expected error envelope, got: {data}"
        assert data["error"]["code"] == "validation_error"
        _ok("cw_follow_href rejects external URL",
            f"'{data['error']['message'][:60]}'")
    except Exception as e:
        _fail("cw_follow_href external URL rejection", e); failures += 1

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(mode: str) -> None:
    print("\nStarting ConnectWise PSA MCP server...")
    os.environ.setdefault("CW_LOAD_VOCABULARY", "0")

    t0 = time.monotonic()
    mcp = create_server()
    print(f"Server ready in {(time.monotonic()-t0)*1000:.0f}ms\n")

    total_failures = 0

    async with Client(mcp) as client:
        if mode in ("all", "describe"):
            total_failures += await test_registry(client)
        if mode in ("all", "live"):
            total_failures += await test_live(client)

    print()
    if total_failures == 0:
        print("All tests passed.")
    else:
        print(f"{total_failures} test(s) FAILED.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode not in ("all", "describe", "live"):
        print("Usage: python scripts/test_local.py [all|describe|live]")
        sys.exit(1)
    asyncio.run(main(mode))
