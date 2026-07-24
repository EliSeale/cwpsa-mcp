"""
Unit tests — RAG search subsystem (docs/Cwpsa_mcp_rag_search_plan.md).

Covers the two pieces with no external dependency:
  * the entity/index registry (§7) and its discovery surface (§2), and
  * the structured-filters -> OData $filter compiler (§3), including the
    injection-prevention invariants (unknown-field rejection, quote escaping,
    datetime validation, list/range shapes).

The Azure AI Search client and live hydration are integration concerns (they
require the search service and ConnectWise) and are exercised elsewhere.

Run: pytest tests/unit/test_search.py -v
"""

from __future__ import annotations

import pytest

from cwpsa import config
from cwpsa.search import client as search_client
from cwpsa.search.registry import (
    SearchFilterError,
    compile_odata_filter,
    get_search_entity,
    searchable_entities,
)
from cwpsa.tools.tier1 import search as search_tool

# ---------------------------------------------------------------------------
# Registry (§7) + discovery surface (§2)
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_tickets_registered(self):
        se = get_search_entity("tickets")
        assert se is not None
        assert se.index == "cw-tickets"
        assert se.cw_entity == "service/tickets"
        assert se.label_field == "summary"

    def test_resolves_cw_entity_path_form(self):
        se = get_search_entity("service/tickets")
        assert se is not None and se.entity == "tickets"

    def test_case_insensitive(self):
        assert get_search_entity("Tickets") is not None

    def test_unknown_entity_is_none(self):
        assert get_search_entity("unicorns") is None

    def test_evidence_select_excludes_vector(self):
        se = get_search_entity("tickets")
        sel = se.evidence_select()
        assert se.vector_field not in sel
        assert se.id_field in sel
        assert se.label_field in sel

    def test_searchable_entities_surface(self):
        surface = searchable_entities()
        assert any(e["entity"] == "tickets" for e in surface)
        tickets = next(e for e in surface if e["entity"] == "tickets")
        assert "company" in tickets["filter_fields"]
        assert tickets["filter_fields"]["closedFlag"]["type"] == "boolean"


# ---------------------------------------------------------------------------
# OData $filter compiler (§3)
# ---------------------------------------------------------------------------

@pytest.fixture
def tickets():
    return get_search_entity("tickets")


class TestOdataCompiler:
    def test_none_and_empty(self, tickets):
        assert compile_odata_filter(tickets, None) is None
        assert compile_odata_filter(tickets, {}) is None

    def test_none_values_skipped(self, tickets):
        assert compile_odata_filter(tickets, {"company": None}) is None

    def test_string_equality(self, tickets):
        assert compile_odata_filter(tickets, {"company": "Acme"}) == "company eq 'Acme'"

    def test_single_quote_escaped(self, tickets):
        # Injection-prevention: quotes are doubled, never break out of the literal.
        assert compile_odata_filter(tickets, {"company": "O'Brien"}) == "company eq 'O''Brien'"

    def test_boolean(self, tickets):
        assert compile_odata_filter(tickets, {"closedFlag": False}) == "closedFlag eq false"
        assert compile_odata_filter(tickets, {"closedFlag": True}) == "closedFlag eq true"

    def test_boolean_type_mismatch_rejected(self, tickets):
        with pytest.raises(SearchFilterError):
            compile_odata_filter(tickets, {"closedFlag": "yes"})

    def test_datetime_range(self, tickets):
        out = compile_odata_filter(
            tickets,
            {"dateEntered": {"from": "2026-06-01T00:00:00Z", "to": "2026-06-30T00:00:00Z"}},
        )
        assert out == (
            "(dateEntered ge 2026-06-01T00:00:00Z and dateEntered le 2026-06-30T00:00:00Z)"
        )

    def test_datetime_open_ended_range(self, tickets):
        out = compile_odata_filter(tickets, {"lastUpdated": {"from": "2026-01-01T00:00:00Z"}})
        assert out == "(lastUpdated ge 2026-01-01T00:00:00Z)"

    def test_datetime_bad_format_rejected(self, tickets):
        with pytest.raises(SearchFilterError):
            compile_odata_filter(tickets, {"dateEntered": {"from": "2026-06-01"}})

    def test_list_becomes_or_of_eq(self, tickets):
        out = compile_odata_filter(tickets, {"status": ["New", "Open"]})
        assert out == "(status eq 'New' or status eq 'Open')"

    def test_empty_list_rejected(self, tickets):
        with pytest.raises(SearchFilterError):
            compile_odata_filter(tickets, {"status": []})

    def test_datetime_list_rejected(self, tickets):
        with pytest.raises(SearchFilterError):
            compile_odata_filter(tickets, {"dateEntered": ["2026-06-01T00:00:00Z"]})

    def test_unknown_field_rejected_with_suggestions(self, tickets):
        with pytest.raises(SearchFilterError) as exc:
            compile_odata_filter(tickets, {"summary": "hello"})
        assert "Unknown filter field" in str(exc.value)

    def test_multiple_filters_anded(self, tickets):
        out = compile_odata_filter(tickets, {"company": "Acme", "closedFlag": False})
        assert out == "company eq 'Acme' and closedFlag eq false"


# ---------------------------------------------------------------------------
# cw_search tool — retrieve-then-hydrate flow (§1, §5) with mocked backends
# ---------------------------------------------------------------------------

async def _get_cw_search_fn():
    """Register the tool on a throwaway server and return its callable."""
    from fastmcp import FastMCP

    mcp = FastMCP(name="test")
    search_tool.register(mcp)
    tool = await mcp.get_tool("cw_search")
    return tool.fn


@pytest.fixture
async def cw_search(monkeypatch):
    # RAG must be enabled for the tool to do anything.
    monkeypatch.setattr(config, "RAG_SEARCH_ENABLED", True)
    return await _get_cw_search_fn()


def _candidates():
    return search_client.SearchOutcome(
        candidates=[
            search_client.Candidate(
                record_id="101", label="VPN down", search_score=1.2,
                reranker_score=2.9, caption="user cannot connect to VPN",
            ),
            search_client.Candidate(
                record_id="102", label="secret ticket", search_score=1.0,
                reranker_score=2.5, caption="restricted",
            ),
        ]
    )


class TestCwSearchFlow:
    async def test_disabled_returns_upstream_unavailable(self, monkeypatch):
        monkeypatch.setattr(config, "RAG_SEARCH_ENABLED", False)
        fn = await _get_cw_search_fn()
        res = await fn(entity="tickets", query="vpn")
        assert res.error.code == "upstream_unavailable"

    async def test_unknown_entity_rejected(self, cw_search):
        res = await cw_search(entity="unicorns", query="x")
        assert res.error.code == "validation_error"

    async def test_requires_query_or_similarity(self, cw_search):
        res = await cw_search(entity="tickets")
        assert res.error.code == "validation_error"

    async def test_drop_on_denial(self, cw_search, monkeypatch):
        # Retrieval returns two candidates; hydration returns only #101 — #102 is
        # dropped (the user's role can't see it), the security property in action.
        async def fake_search(se, **kw):
            return _candidates()

        async def fake_cw_get(path, **params):
            return [{"id": 101, "summary": "VPN down", "_info": {"lastUpdated": "t"}}]

        monkeypatch.setattr(search_client, "search", fake_search)
        monkeypatch.setattr(search_tool, "_cw_get", fake_cw_get)

        res = await cw_search(entity="tickets", query="vpn", hydrate_limit=2)
        assert res["total_candidates"] == 2
        assert res["dropped_count"] == 1
        assert len(res["results"]) == 1
        r = res["results"][0]
        assert r["record"]["id"] == 101
        # Authoritative record is separate from the (untrusted) evidence block.
        assert r["evidence"]["id"] == 101
        assert r["evidence"]["caption"] == "user cannot connect to VPN"

    async def test_hydrate_false_returns_evidence_only(self, cw_search, monkeypatch):
        async def fake_search(se, **kw):
            return _candidates()

        monkeypatch.setattr(search_client, "search", fake_search)
        res = await cw_search(entity="tickets", query="vpn", hydrate=False)
        assert res["hydrated"] is False
        assert len(res["candidates"]) == 2
        assert "record" not in res["candidates"][0]

    async def test_zero_hits(self, cw_search, monkeypatch):
        async def fake_search(se, **kw):
            return search_client.SearchOutcome(candidates=[])

        monkeypatch.setattr(search_client, "search", fake_search)
        res = await cw_search(entity="tickets", query="nothing")
        assert res["total_candidates"] == 0
        assert res["results"] == []

    async def test_hydrate_limit_capped(self, cw_search, monkeypatch):
        async def fake_search(se, **kw):
            return _candidates()

        async def fake_cw_get(path, **params):
            return []

        monkeypatch.setattr(config, "SEARCH_HYDRATE_CEILING", 1)
        monkeypatch.setattr(search_client, "search", fake_search)
        monkeypatch.setattr(search_tool, "_cw_get", fake_cw_get)
        res = await cw_search(entity="tickets", query="vpn", hydrate_limit=999)
        # ceiling=1 -> only the top candidate is hydrated (then dropped here).
        assert res["total_candidates"] == 2
        assert res["dropped_count"] == 1

    async def test_all_dropped_explains_access_boundary(self, cw_search, monkeypatch):
        async def fake_search(se, **kw):
            return _candidates()

        async def fake_cw_get(path, **params):
            return []  # nothing accessible

        monkeypatch.setattr(search_client, "search", fake_search)
        monkeypatch.setattr(search_tool, "_cw_get", fake_cw_get)
        res = await cw_search(entity="tickets", query="vpn", hydrate_limit=2)
        assert res["results"] == []
        assert res["dropped_count"] == 2
        assert "none of the" in res["message"]
