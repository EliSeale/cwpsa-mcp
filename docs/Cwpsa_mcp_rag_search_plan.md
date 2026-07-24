# ConnectWise PSA MCP Server: RAG Search Subsystem (Implementation Plan)

Version 0.2. A build plan for semantic/hybrid search across ConnectWise entities via Azure AI Search. This is a plan, not code. It is grounded in the deployed `cw-tickets` index schema and the server's existing auth and response-governance model.

## 1. Purpose and the core principle

The RAG subsystem lets an agent answer questions that keyword filters cannot, for example "find tickets similar to this one," "have there been any M365 issues for Acme recently," or "which ticket involved a network change for Company X." It does this with hybrid semantic retrieval over an Azure AI Search index.

The governing principle, which shapes every design decision below: **the index is a discovery layer, never a source of truth or a data surface.** A search returns record IDs plus small justification snippets. It never returns authoritative field values to the agent. Immediately after retrieval, the tool hydrates the chosen IDs by calling ConnectWise live, under the caller's impersonated member. Three properties fall out of this and are the reason to build it this way:

1. **Freshness.** The index is eventually consistent and will lag reality, especially during buildout. Hydrating by ID against the live API means the agent always acts on current data, and the stale index only ever influences *which* records to look at, never *what they say*.
2. **Security.** Because retrieval returns only IDs and the live hydration runs under the user's ConnectWise security role, the index can hold sensitive text that a given user should not see. If a search surfaces an ID the user cannot access, the hydration call is denied by ConnectWise and the record is dropped from the result. The index never leaks record contents past the ConnectWise permission boundary.
3. **Consistency with the rest of the server.** Hydration reuses the existing Tier 1 read path (`cw_get`/`cw_query`), so response governance, untrusted-content handling, and impersonation all apply unchanged.

## 2. Tool shape: one generic search tool, entity-parameterized

Build a single retrieval tool, the semantic sibling of `cw_query`, rather than one tool per entity. This keeps the tool count flat as indexes are added (tickets today, projects and opportunities next) and matches the genericization principle already used in Tier 1.

Proposed surface:

```
cw_search(
    entity,             # tickets | projects | opportunities | companies | ...
    query,              # natural-language text (vectorized + keyword)
    filters,            # structured metadata filters (company, date range, board, status, ...)
    similar_to_id,      # optional: similarity mode, seeds the query from this record
    top_k,              # candidate count from the index (evidence only), hard-capped
    hydrate_limit,      # how many top candidates to fetch live; AGENT-CONTROLLED (see note)
    hydrate,            # default true: auto-fetch live records by ID after retrieval
)
```

- `entity` maps to an index name through a server-side registry (`tickets -> cw-tickets`, `projects -> cw-projects`, ...). The caller never passes a raw index name.
- Free-text search ("M365 issues for Acme") and similarity ("tickets like #967694") are the same tool; similarity sets `similar_to_id`, and the server builds the query vector from that record's indexed content.
- `hydrate=true` (default) performs the ID lookup automatically (Section 5). `hydrate=false` returns candidate IDs plus evidence only, for cases where the agent wants to triage before fetching.
- **`hydrate_limit` is set by the agent, not fixed by the server.** Hydrated records carry full ConnectWise note history, which can be large, so the number of records to fetch live is a deliberate cost/context tradeoff the model should make per query. The tool description tells the model this explicitly: retrieving many records pulls large note bodies and consumes context, so ask for few when you only need the top matches and more only when breadth matters. The server still enforces a hard ceiling on `hydrate_limit` so an over-eager request cannot blow the response budget or the outbound rate budget (Section 5).

Discovery: extend `cw_describe` to list which entities are searchable and what filter fields each index supports, so the agent learns the search surface the same way it learns entity fields.

## 3. Retrieval design (Azure AI Search)

The deployed `cw-tickets` index already has the right bones: BM25 similarity, an HNSW vector profile (`ticket-vector-profile`, cosine, 3072-dim from `text-embedding-3-large`), and a semantic configuration (`ticket-semantic-config`). Use all three layers, because the target queries need different strengths.

- **Hybrid retrieval (keyword + vector, fused with RRF).** Send both the BM25 query and the vectorized query in one request. Keyword recall catches exact tokens (company names, "M365", ticket numbers, acronyms); vector recall catches paraphrase and semantic intent ("angry customer", "network change"). Reciprocal Rank Fusion merges them. This is the default retrieval mode and the industry standard for RAG over mixed structured/free text.
- **Semantic ranker (L2 rerank).** Enable the semantic configuration on the query so the top candidates are re-scored by the cross-encoder ranker. This is what makes tone- and nuance-based queries ("seems frustrated", "change was made") rank well, because the ranker reads query and document together rather than comparing vectors. Feed it a healthy candidate set (Azure reranks up to the top 50); do not pre-filter so aggressively that the ranker is starved.
- **Vector-only mode** is the natural fit for pure similarity (`similar_to_id`), where there is no user text, just the seed record's vector.

The vector field (`contentVector`) is built from the concatenated ticket notes rolled up to the ticket, so a query like "John Smith networking issues" vector-matches against the note history and returns the ticket ID. That is the correct granularity for ticket-level similarity and incident-finding. Keep `content` retrievable so it can supply the evidence snippet (Section 4); keep `contentVector` non-retrievable (it already is).

### Filtering

Every structured field in the index is filterable, which is what enables the "recent M365 issues for Acme" pattern: a semantic query on the text plus hard filters on `company` and `dateEntered`/`lastUpdated`. Guidance:

- Compile `filters` from a structured object into an OData `$filter` string server-side; never accept a raw filter string from the agent (same injection-prevention stance as the Tier 1 filter DSL).
- Prefer **pre-filtering** (filter before vector search) for scoping filters like company and date, so the ranker's candidate set is already relevant. Watch the tension with the ranker's need for ~50 candidates: a filter so narrow that it yields a handful of documents underserves the reranker. For very narrow scopes this is fine; just be aware the reranker adds little when there are only a few candidates.
- Common filter fields available today: `company`, `board`, `status`, `type`, `priority`, `closedFlag`, `isChildTicket`, `recordType`, `dateEntered`, `lastUpdated`, plus location fields. Date filtering uses the `Edm.DateTimeOffset` fields.

### Similarity mode

For `similar_to_id`: fetch the seed record's vector (or re-embed its `content`), run a vector query with `k = top_k`, optionally constrained by filters (for example, similar tickets *for the same company* or *on the same board*). Return the neighbors as candidate IDs with their scores.

## 4. Evidence, not data

The agent needs to explain *why* a record matched without the tool surfacing authoritative field values. Return a compact, clearly-labeled **evidence** block per candidate, distinct from the hydrated record:

- The reranker score and the search score (so the agent can rank and threshold).
- A **caption/highlight** snippet from Azure AI Search (the extractive `@search.captions`/highlights, like the printer-setup example), which shows the matched passage. This is the "why it's similar" material.
- A minimal label for human readability (for tickets, `summary`; for projects, the name), enough to disambiguate in a list.

Treat evidence as **untrusted display text**, never as instructions or authoritative state. It is wrapped/spotlighted the same way record free-text is on the Tier 1 read path (Section 6). The authoritative values come only from hydration. Practically: the evidence snippet is "here is the passage that matched," and the hydrated record is "here is the current truth," and the response keeps those two visibly separate so the model does not treat a stale snippet as fact.

## 5. Hydration: IDs to live records

After retrieval, and by default, the tool hydrates:

1. Take the ordered candidate IDs from the search.
2. Fetch them live from ConnectWise, under the caller's impersonated member, using the existing Tier 1 read path. **Hydrate with a single batched `cw_query` per entity using `id in (...)`**, not N per-record `cw_get` calls. This is the deliberate choice: one query handles a single record or many with no code change, keeps the call count (and mint/rate budget) low, and reuses the existing filter compiler, projection, and size governance unchanged. Because the compiler already has the `/search` POST fallback for long `in (...)` lists, a large batch of IDs stays within URL limits automatically.
3. **Drop, do not error, on denial.** If ConnectWise returns not-authorized/404 for an ID (the user's role cannot see it, or it was deleted since indexing), omit it from the results and note the count of dropped items. This is the security property in action: the index proposed a candidate, the live permission check filtered it.
4. Return the surviving records (authoritative) paired with their evidence (why they matched), ordered by rerank score.

Bound the hydration: the agent sets `hydrate_limit` (how many of the ranked candidates to fetch), and the server clamps it to a hard ceiling. Because hydration is one batched `id in (...)` query, the cost is one API call regardless of batch size, but the *response* size still grows with the number of records and their note bodies, so the ceiling protects the response budget. If the agent wants more than the ceiling, it pages by re-querying with the next slice of candidate IDs.

Staleness handling: because hydration is authoritative, a record whose indexed snippet is out of date is simply corrected on fetch. Optionally include the index `lastUpdated` alongside the live record so the agent can see how stale the matched snippet was.

## 6. Security and access control

- **Azure AI Search auth uses RBAC with the MCP server's managed identity, not an API key.** Assign the Container App's managed identity the data-plane role **Search Index Data Reader** on the search service (reader is sufficient; the server only queries, never writes the index). Enable RBAC (role-based) auth on the search service and stop using the admin/query keys from the server. The one exception is the vectorizer: the index's `foundry-embedding-large` vectorizer currently holds an Azure OpenAI API key; prefer switching that to managed-identity auth as well so no key is stored.
- **The ConnectWise permission boundary is enforced at hydration, not in the index.** This is the deliberate v1 design: there is **no index-level ACL**; the impersonated live fetch is the only access gate. Two things make that acceptable for v1. First, the index is seeded with **minimal sensitive content to start**, so the exposure of an evidence snippet is low-risk by policy, not just by mechanism. Second, the hydration gate still fully protects the authoritative record: even if a snippet is broad, the full record is only returned when the user's ConnectWise role allows the live fetch. As indexes expand to hold more sensitive text, revisit this (see Section 12) and add document-level security to the specific indexes that need it, rather than to all of them. Note that index read access (the server's managed identity) is broad by design in v1, and the real ACL is the ConnectWise role applied during hydration.
- **Untrusted content.** Everything retrieved (captions, `content`, summaries) is user-generated ConnectWise text and is treated as data, never instructions, identical to the Tier 1 untrusted-content stance. Never let a snippet drive a write or a tool call target.
- **PII in the index.** The index contains contact names, emails, phone numbers, and note bodies. Since evidence snippets surface some of this to the agent, keep the evidence minimal (the matched passage plus score), and rely on hydration for full detail so the ConnectWise role governs exposure of the complete record.

## 7. The entity/index registry

A small server-side map is the only per-entity configuration:

```
tickets       -> { index: "cw-tickets",       cw_entity: "service/tickets",     label: "summary" }
projects      -> { index: "cw-projects",      cw_entity: "project/projects",    label: "name" }
opportunities -> { index: "cw-opportunities", cw_entity: "sales/opportunities", label: "name" }
companies     -> { index: "cw-companies",     cw_entity: "company/companies",   label: "name" }
```

Each entry ties together: the Azure AI Search index name, the ConnectWise entity path used for hydration, the display label field, and (optionally) the list of filter fields that index supports. Adding a new searchable entity is one registry entry plus the index itself; the tool code does not change. This is what keeps it a single tool.

## 8. Index build and freshness (ingestion)

Out of scope for the tool, but the plan depends on it, so state the contract:

- **Ingestion pipeline** populates and refreshes each index from ConnectWise. Ticket content is the concatenation of notes up to the ticket entity (already the case). Drive incremental refresh off `lastUpdated` polling and/or ConnectWise callbacks, so the index converges toward current without full rebuilds.
- **Embedding** uses `text-embedding-3-large` (3072-dim), consistent across all indexes so vectors are comparable and the same vectorizer config is reused.
- **Derived-field enrichment (recommended, later).** Precompute classification fields at ingestion, for example a sentiment score or an incident-type tag (network, M365, printer, security). This converts fuzzy semantic asks into filter-assisted queries: "angry customer" becomes a sentiment filter plus semantic query, "network change" becomes an incident-type filter, which is far more reliable than vector similarity alone. This is the standard Azure AI Search enrichment-skillset pattern and it directly strengthens the example queries this subsystem is for.
- **Staleness is expected and acceptable** precisely because hydration corrects it. The index does not need to be real-time; it needs to be good enough to propose the right candidate IDs.

## 9. Response governance

The search result reuses the server's response contract:

- Small by default: `top_k` capped, evidence snippets truncated to a bounded length.
- Summary-first envelope: total candidate count, then the hydrated records with evidence, then a note if candidates were dropped on permission/hydration failure, then a paging cursor if more candidates exist.
- Compact evidence: rerank score, one caption/highlight, one label field. Not the raw index document.
- `concise`/`detailed` verbosity consistent with the read tools.

## 10. Failure modes and their handling

- **Search service unavailable** -> structured `upstream_unavailable`, retryable, same envelope as a ConnectWise outage.
- **Index does not exist for the requested entity** -> `validation_error` naming the searchable entities (from the registry).
- **Zero hits** -> empty result with a clear "no matches" status; the agent should not fabricate.
- **All candidates dropped at hydration** (permission or deletion) -> return empty with a note that N candidates were found but none were accessible or current, so the agent can explain the access boundary rather than claim nothing exists.
- **Embedding/vectorizer failure** (Azure OpenAI throttle) -> fall back to keyword-only retrieval for that call rather than failing outright, and flag reduced quality.

## 11. Phasing

- **Phase A (tickets, the existing index).** `cw_search(entity="tickets", ...)` with hybrid + semantic ranker + metadata filters, evidence block, and auto-hydration via `cw_get` under impersonation. RBAC (managed identity + Search Index Data Reader) for index access. Prove the retrieve-then-hydrate loop and the drop-on-denial security property.
- **Phase B (more entities).** Add projects, opportunities, companies as indexes; extend the registry. Add similarity mode (`similar_to_id`). No tool changes beyond registry entries.
- **Phase C (enrichment).** Add sentiment/incident-type derived fields at ingestion; expose them as filters so the fuzzy example queries become filter-assisted. Optionally adopt Azure AI Search agentic retrieval (automatic query decomposition) for multi-part questions.

## 12. Resolved decisions

- **Hydration is a single batched `cw_query` with `id in (...)`.** Decided. One query serves both the single-record and many-record cases with no code change, minimizes call and rate-budget cost, and reuses the existing compiler (including the `/search` POST fallback for long ID lists). Per-ID `cw_get` is not used for hydration.
- **Hydration count is agent-controlled via `hydrate_limit`.** Decided. Because full records carry large note histories, the model chooses how many to fetch, guided by explicit tool-description context that more records means more (and larger) note bodies in context. The server enforces a hard ceiling to protect the response budget; beyond it, the agent pages.
- **No index-level ACL in v1.** Decided. Indexes start with minimal sensitive content, and the hydration-time ConnectWise role check is the access gate. Document-level security will be added later, per index, only if and when an index holds text sensitive enough that even an evidence snippet must be access-controlled.

## 13. Defaults

- **`hydrate_limit` default is 5**, with a server-enforced hard ceiling above it. The agent may request fewer or more up to the ceiling; 5 is the starting point because it covers the common "show me the top matches" case while keeping large note bodies out of context by default. Tune the ceiling from telemetry (truncation frequency, response bytes) if needed.
- **Query embedding is handled by Azure AI Search natively** via the index's configured vectorizer (`foundry-embedding-large`), so the server sends text and lets the service embed it. There is no separate embedding call in the server for either free-text or `similar_to_id` queries.