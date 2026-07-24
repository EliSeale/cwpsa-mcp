"""
Azure AI Search REST client (§3, §6 of the RAG plan).

Retrieval layers (all three used, because the target queries need different
strengths — §3):
  * Hybrid retrieval — BM25 keyword + integrated vectorization, fused with RRF.
    The service embeds the query text natively via the index vectorizer, so the
    server sends text and never makes a separate embedding call (§13).
  * Semantic ranker (L2) — the index's semantic configuration re-scores the top
    candidates with a cross-encoder, which is what makes tone/nuance queries rank.
  * Vector-only — the natural fit for pure similarity (`similar_to_id`), where
    there is no user text, just the seed record's indexed content.

Auth is RBAC with the Container App's **managed identity** — role Search Index
Data Reader — not an API/query key (§6).  A bearer token for the search data
plane is acquired via DefaultAzureCredential and cached until shortly before it
expires.  There is no key stored anywhere in the server.

Graceful degradation (§10): if the query's vectorization/semantic pass fails
(e.g. the Azure OpenAI vectorizer is throttled), the client retries once as a
keyword-only BM25 query and flags the reduced quality, rather than failing the
call outright.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from cwpsa import config
from cwpsa.search.registry import SearchEntity

log = logging.getLogger(__name__)


class SearchUnavailable(Exception):
    """The search service could not be reached / is not configured.

    Mapped by the tool to an ``upstream_unavailable`` envelope (retryable, §10).
    """


class SearchNotConfigured(SearchUnavailable):
    """No Azure AI Search endpoint is configured (RAG dormant)."""


@dataclass
class Candidate:
    """One retrieved candidate — evidence only, never authoritative data (§4)."""

    record_id: str
    label: str | None
    search_score: float | None
    reranker_score: float | None
    caption: str | None
    index_last_updated: str | None = None


@dataclass
class SearchOutcome:
    """Result of a retrieval: ordered candidates + quality flags."""

    candidates: list[Candidate]
    degraded: str | None = None  # e.g. "keyword_only" when the vector pass fell back


# ---------------------------------------------------------------------------
# Managed-identity token cache (RBAC, §6)
# ---------------------------------------------------------------------------
_token_value: str | None = None
_token_expiry: float = 0.0
_token_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    return _token_lock


async def _get_bearer_token() -> str:
    """Return a cached data-plane bearer token, refreshing shortly before expiry.

    DefaultAzureCredential.get_token is synchronous, so it runs in a worker
    thread to avoid blocking the event loop.
    """
    global _token_value, _token_expiry
    now = time.time()
    if _token_value and now < _token_expiry - 120:
        return _token_value

    async with _get_lock():
        now = time.time()
        if _token_value and now < _token_expiry - 120:
            return _token_value
        credential = config.get_azure_credential()

        def _fetch() -> Any:
            return credential.get_token(config.AZURE_SEARCH_SCOPE)

        try:
            token = await asyncio.to_thread(_fetch)
        except Exception as exc:  # credential/RBAC failure
            raise SearchUnavailable(
                f"Could not obtain a managed-identity token for Azure AI Search: {exc}"
            ) from exc
        _token_value = token.token
        _token_expiry = float(token.expires_on)
        return _token_value


# ---------------------------------------------------------------------------
# HTTP client (bound to the running loop, mirrors integration/client.py)
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _get_client() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        _client_loop = loop
    return _client


async def close_client() -> None:
    global _client, _client_loop
    if _client is not None:
        await _client.aclose()
        _client = None
        _client_loop = None


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

def _build_body(
    se: SearchEntity,
    *,
    query_text: str | None,
    odata_filter: str | None,
    top_k: int,
    use_vector: bool,
    use_semantic: bool,
) -> dict[str, Any]:
    """Assemble a POST /docs/search body for the requested retrieval mode."""
    body: dict[str, Any] = {
        "top": top_k,
        "count": True,
        "select": ",".join(se.evidence_select()),
    }

    # Keyword (BM25) leg — always present when there is user text.
    body["search"] = query_text if query_text else "*"

    if use_vector:
        # Integrated vectorization: the service embeds this text via the index
        # vectorizer (§13). For vector-only similarity, query_text is the seed
        # record's indexed content and body["search"] is "*".
        vector_text = query_text if query_text else ""
        if vector_text:
            body["vectorQueries"] = [
                {
                    "kind": "text",
                    "text": vector_text,
                    "fields": se.vector_field,
                    "k": top_k,
                }
            ]

    if use_semantic and se.semantic_config:
        body["queryType"] = "semantic"
        body["semanticConfiguration"] = se.semantic_config
        body["captions"] = "extractive"
        body["highlightPreTag"] = ""
        body["highlightPostTag"] = ""

    if odata_filter:
        body["filter"] = odata_filter

    return body


def _parse_response(se: SearchEntity, payload: dict[str, Any]) -> list[Candidate]:
    """Turn a search response into evidence-only Candidate objects (§4)."""
    out: list[Candidate] = []
    for doc in payload.get("value", []):
        rid = doc.get(se.id_field)
        if rid is None:
            continue
        caption: str | None = None
        caps = doc.get("@search.captions")
        if isinstance(caps, list) and caps and isinstance(caps[0], dict):
            caption = caps[0].get("highlights") or caps[0].get("text")
        if caption:
            caption = caption[: config.SEARCH_CAPTION_MAX_CHARS]
        out.append(
            Candidate(
                record_id=str(rid),
                label=doc.get(se.label_field),
                search_score=doc.get("@search.score"),
                reranker_score=doc.get("@search.rerankerScore"),
                caption=caption,
                index_last_updated=doc.get("lastUpdated") or doc.get("dateEntered"),
            )
        )
    # Order by reranker score when present, else raw search score (§4/§9).
    out.sort(
        key=lambda c: (
            c.reranker_score if c.reranker_score is not None else -1.0,
            c.search_score if c.search_score is not None else -1.0,
        ),
        reverse=True,
    )
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def _post_search(se: SearchEntity, body: dict[str, Any]) -> dict[str, Any]:
    """POST to the index's /docs/search endpoint with the RBAC bearer token."""
    if not config.AZURE_SEARCH_ENDPOINT:
        raise SearchNotConfigured("AZURE_SEARCH_ENDPOINT is not set.")

    token = await _get_bearer_token()
    url = (
        f"{config.AZURE_SEARCH_ENDPOINT}/indexes/{se.index}/docs/search"
        f"?api-version={config.AZURE_SEARCH_API_VERSION}"
    )
    client = _get_client()
    try:
        resp = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
        raise SearchUnavailable(f"Azure AI Search network error: {exc}") from exc

    if resp.status_code == 404:
        # Index missing — surfaced distinctly by the tool (validation_error, §10).
        raise httpx.HTTPStatusError(
            f"Index '{se.index}' not found.", request=resp.request, response=resp
        )
    if resp.status_code >= 500:
        raise SearchUnavailable(
            f"Azure AI Search server error {resp.status_code}: {resp.text[:300]}"
        )
    resp.raise_for_status()
    payload: dict[str, Any] = resp.json()
    return payload


async def search(
    se: SearchEntity,
    *,
    query_text: str | None,
    odata_filter: str | None,
    top_k: int,
    vector: bool = True,
    semantic: bool = True,
) -> SearchOutcome:
    """Run hybrid + semantic retrieval, with a keyword-only fallback (§3, §10).

    Args:
        se:           target search entity (from the registry).
        query_text:   natural-language query, or the seed content for similarity.
        odata_filter: pre-compiled OData $filter, or None.
        top_k:        candidate count fed to the reranker (already capped by caller).
        vector:       include the integrated-vectorization leg (hybrid).
        semantic:     apply the semantic (L2) reranker.

    Returns a SearchOutcome with ordered evidence-only candidates and a
    ``degraded`` flag when a fallback path was taken.
    """
    body = _build_body(
        se,
        query_text=query_text,
        odata_filter=odata_filter,
        top_k=top_k,
        use_vector=vector,
        use_semantic=semantic,
    )
    try:
        payload = await _post_search(se, body)
        return SearchOutcome(candidates=_parse_response(se, payload))
    except SearchUnavailable:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            raise  # index-missing — let the tool classify it
        # 400-class error from the vector/semantic pass → fall back to keyword-only.
        if vector or semantic:
            log.warning(
                "[search] hybrid/semantic query failed (%s) — falling back to keyword-only",
                status,
            )
            kw_body = _build_body(
                se,
                query_text=query_text,
                odata_filter=odata_filter,
                top_k=top_k,
                use_vector=False,
                use_semantic=False,
            )
            payload = await _post_search(se, kw_body)
            return SearchOutcome(
                candidates=_parse_response(se, payload), degraded="keyword_only"
            )
        raise


async def fetch_seed_content(se: SearchEntity, seed_id: str) -> str | None:
    """Fetch the indexed ``content`` of a seed document for similarity mode (§3).

    Similarity uses integrated vectorization: rather than reading the (non-
    retrievable) raw vector, we re-embed the seed's indexed content at query time
    by passing it as the vector query text.  Returns None if the seed is absent
    or its content is not retrievable.
    """
    if not config.AZURE_SEARCH_ENDPOINT:
        raise SearchNotConfigured("AZURE_SEARCH_ENDPOINT is not set.")
    token = await _get_bearer_token()
    url = (
        f"{config.AZURE_SEARCH_ENDPOINT}/indexes/{se.index}/docs/search"
        f"?api-version={config.AZURE_SEARCH_API_VERSION}"
    )
    body = {
        "search": "*",
        "filter": f"{se.id_field} eq '{str(seed_id).replace(chr(39), chr(39) * 2)}'",
        "select": se.content_field,
        "top": 1,
    }
    client = _get_client()
    try:
        resp = await client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        raise
    except Exception as exc:
        raise SearchUnavailable(f"Azure AI Search network error: {exc}") from exc
    values = resp.json().get("value", [])
    if not values:
        return None
    content: str | None = values[0].get(se.content_field)
    return content
