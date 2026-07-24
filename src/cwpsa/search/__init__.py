"""
RAG / semantic search subsystem (docs/Cwpsa_mcp_rag_search_plan.md).

The governing principle: **the index is a discovery layer, never a source of
truth or a data surface.** A search returns record IDs plus small justification
snippets; authoritative values only ever come from hydrating those IDs live
against ConnectWise under the caller's impersonated member.

Modules:
  registry.py  -- entity -> Azure AI Search index map + safe OData filter compiler.
  client.py    -- Azure AI Search REST client (managed-identity RBAC, hybrid +
                  semantic ranker, vector similarity, keyword-only fallback).

The tool that ties these together lives in cwpsa.tools.tier1.search (cw_search).
"""

from cwpsa.search.registry import (
    SearchEntity,
    SearchFilterError,
    compile_odata_filter,
    get_search_entity,
    searchable_entities,
)

__all__ = [
    "SearchEntity",
    "SearchFilterError",
    "compile_odata_filter",
    "get_search_entity",
    "searchable_entities",
]
