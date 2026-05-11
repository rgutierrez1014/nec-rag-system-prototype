# NEC Natural Language Search — Prototype Specification

## Table of Contents

- [Overview](#overview)
- [Goals](#goals)
- [Architecture](#architecture)
- [Data model](#data-model)
- [Seed datasets](#seed-datasets)
- [Tech stack](#tech-stack)
- [Architectural decisions](#architectural-decisions)
- [Query flow](#query-flow)
- [Ingestion pipeline](#ingestion-pipeline)
- [Hybrid retrieval](#hybrid-retrieval)
- [Guardrails](#guardrails)
- [PII pre-processing](#pii-pre-processing)
- [Abuse protection](#abuse-protection)
- [Evaluation](#evaluation)
- [Observability](#observability)
- [Frontend](#frontend)
- [Infrastructure](#infrastructure)
- [Implementation order](#implementation-order)
- [Cost estimate](#cost-estimate)
- [County expansion playbook](#county-expansion-playbook)
- [Resource schema learnings](#resource-schema-learnings)
- [Blog series structure](#blog-series-structure)

---

## Overview

This document specifies the prototype implementation of an AI-powered natural language search service for the Neurodivergent Equity Coalition platform. The prototype validates the full RAG (Retrieval-Augmented Generation) pipeline end-to-end — ingestion, chunking, embedding, hybrid retrieval, re-ranking, and synthesized responses with citations — using public seed data scoped to San Francisco County.

The prototype is a **standalone service in its own repository**, deployed on its own infrastructure, and exposed as an API. The main NEC platform will eventually integrate with this API. A Next.js frontend serves as a demo client for recruiters, users, and testing, but the API is the product.

This prototype is not a throwaway tech demo. The architecture, retrieval pipeline, ingestion transforms, evaluation harness, and guardrails are designed to carry forward directly into the production system described in the parent specification (`NEC_AI_Natural_Language_Search_Spec.md`). Decisions that differ between prototype and production are documented with explicit migration paths.

---

## Goals

1. **Demonstrate the full RAG pipeline end-to-end:** ingestion, chunking, embedding, hybrid retrieval, re-ranking, and synthesized responses with citations.
2. **Validate the query interface for two primary user intents:** informational queries ("what does an IEP cover in San Francisco?") and provider lookup queries ("find an occupational therapist in the Sunset District").
3. **Produce schema learnings for the Resource model:** the curated resource ingestion surfaces what metadata is needed, informing the eventual Django model in the main platform and the SRIP Resource schema.
4. **Produce a deployable, documented project** that real users and recruiters can interact with, and that can be pointed at real platform data when SRIP is live.
5. **Document a county expansion playbook** — adding a new county to the system should be a repeatable process, consistent with the nonprofit's county-by-county scaling model.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Hetzner CX33 VPS (Dokploy-managed)                 │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │ FastAPI Service                               │   │
│  │                                               │   │
│  │  /query            → PII pre-processing       │   │
│  │                    → hybrid retrieval          │   │
│  │                    → cross-encoder rerank      │   │
│  │                    → Anthropic API (Haiku)     │   │
│  │                    → response + citations      │   │
│  │                                               │   │
│  │  /tasks/ingest-npi                            │   │
│  │  /tasks/ingest-resources                      │   │
│  │  /tasks/rebuild-bm25-index                    │   │
│  │  /tasks/re-embed                              │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ┌────────────────┐  ┌──────────────────────────┐   │
│  │ Postgres +      │  │ Ollama                    │   │
│  │ pgvector        │  │ (nomic-embed-text)        │   │
│  └────────────────┘  └──────────────────────────┘   │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │ Next.js Frontend (demo client)                │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
         │                            │
         │ Cloudflare                 │ Anthropic API
         │ (DDoS, bot protection)     │ (Haiku 3.5)
```

The FastAPI service is the core of the system. It handles both query-time operations (retrieval and synthesis) and ingestion tasks (triggered via `/tasks/` endpoints). Postgres + pgvector stores vector embeddings and metadata. Ollama runs the embedding model for both ingestion and query-time embedding. The Next.js frontend is a thin demo client that calls the same API the main platform will eventually call.

All components run as Docker containers managed by Dokploy on a single Hetzner VPS, isolated from the main NEC platform's infrastructure.

---

## Data model

### Practice-centric design

The primary searchable unit is a **Practice document**, not a Professional. This mirrors the main NEC platform's actual data model, where a Practice holds the location, services, specialties, presence types, certifications, and links to Professionals and Spaces. When a user asks "find a therapist near me who specializes in autism evaluations," they are searching for a Practice.

Structuring the prototype's seed data as Practice-shaped documents means the ingestion transform, embedding schema, retrieval patterns, and query logic are directly reusable when the prototype is pointed at real SRIP data from the main platform. The alternative — indexing flat Professional records — would require rewriting the retrieval layer for production.

A Practice document in the vector store contains:

- Practice name and description
- Practice type (healthcare / general)
- Address (street, city, state, ZIP)
- Services offered (mapped from NPI taxonomy codes)
- Specialties
- Presence types (in-person, remote)
- Professional roster (names, titles, credentials)
- NPI number
- Neighborhood (SF neighborhood name, derived via reverse geocoding at ingestion time — see [Neighborhood enrichment](#neighborhood-enrichment))

### Object type registry

Each searchable object type (Practices, Resources) is defined as a declarative configuration entry specifying: which database table to query, which fields to embed, which metadata fields are filterable, the chunking strategy, and the retrieval chain configuration. Adding a new object type means adding a config entry and an ingestion transform, not modifying core retrieval logic.

This pattern is established with two types in the prototype (Practices, Resources) and followed for each subsequent type added in production (Organizations, Spaces).

---

## Seed datasets

Both datasets are scoped to San Francisco County for the prototype. This provides a coherent user experience — providers and resources cover the same geography — and aligns with the nonprofit's county-by-county scaling model. See [County expansion playbook](#county-expansion-playbook) for how additional counties are added.

### NPI registry (provider data)

The National Provider Identifier registry, maintained by CMS and available as a full public download at nppes.cms.hhs.gov, provides structured provider records for every licensed healthcare provider in the United States.

For the prototype, the dataset is filtered to **San Francisco County** behavioral health providers using taxonomy codes covering: psychologists, licensed counselors, marriage and family therapists, psychiatrists, occupational therapists, speech-language pathologists, and psychiatric nurse practitioners.

Each NPI record is transformed into a Practice-shaped document. The NPI fields that map to the Practice model are:

| NPI field | Maps to |
|-----------|---------|
| NPI number | `npi_number` |
| Provider name | Professional roster entry |
| Taxonomy code | Services (via taxonomy-to-service mapping) |
| Business practice address | Practice address fields |
| Enumeration/last update date | Metadata |

Fields the NPI registry does not include — insurance accepted, languages, neurodivergent-affirming credentials — are left empty and noted as fields that SRIP partner data will eventually populate.

### Curated resource corpus (informational content)

A hand-curated list of 50-100 high-quality informational pages from community-trusted organizations: ASAN (autisticadvocacy.org), The Arc (thearc.org), CHADD (chadd.org), Understood (understood.org), and similar. The corpus includes two types of content:

- **National foundational content:** Articles about IEP rights under IDEA, therapy preparation guides, sensory accommodation overviews, diagnostic process explainers. These are relevant regardless of geography and will benefit users in every county as the system expands.
- **San Francisco-specific content:** Resources about navigating SF Unified School District, county behavioral health services, local support groups, Bay Area-specific provider guides. These provide the localized relevance that is the core value proposition of the NEC platform.

Pages are selected manually — a curated URL list, not a site crawl — to ensure content quality and exclude irrelevant page types (event listings, donation pages, staff bios). Source exclusions established for the platform apply here: nothing from autismspeaks.org, nothing from ABA-affiliated sources.

National resources accumulate over time and benefit all counties immediately when new counties are added. County-specific resources are curated per expansion. New resources can be added at any time by running the ingestion task endpoint with new URLs.

---

## Tech stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Orchestration | FastAPI + direct Anthropic SDK | Matches existing codebase patterns (tasks service); no framework abstraction overhead |
| Vector store | Postgres + pgvector | Same database handles vectors, metadata, and full-text search; production-portable to Cloud SQL |
| Keyword search | rank-bm25 | Industry-standard BM25 library; production migrates to Postgres FTS (see [migration path](#bm25-production-migration-path)) |
| Embeddings | Ollama + nomic-embed-text | Self-hosted, no external dependency on retrieval path; same model used in production |
| Synthesis | Claude Haiku 3.5 via Anthropic API | Cost-effective for constrained synthesis; upgrade path: Haiku 3.5 → Haiku 4 → Sonnet |
| Re-ranking | sentence-transformers + cross-encoder/ms-marco-MiniLM-L-6-v2 | Lightweight cross-encoder (~90MB), no GPU required; CPU-only PyTorch build |
| PII pre-processing | Microsoft Presidio + spaCy (en_core_web_md) | Full implementation — real users will query the system |
| Observability | LangSmith (free tier) | Query tracing and retrieval inspection; works without LangChain |
| Evaluation | RAGAS | Open-source, runs locally, measures context recall / faithfulness / answer relevance |
| Frontend | Next.js (standalone demo client) | Thin demo layer; the API is the product |
| Infrastructure | Hetzner CX33 + Dokploy + Cloudflare | $8.59/mo VPS, separate from main app |

### Dependency notes

- **PyTorch:** Install the CPU-only build (`pip install torch --index-url https://download.pytorch.org/whl/cpu`) to minimize disk footprint (~200-300MB vs ~1.5-2GB for the full CUDA build). The CX33 has no GPU; CUDA is unnecessary.
- **spaCy model:** Use `en_core_web_md` (medium model, ~40MB on disk, ~150MB in memory) for Presidio's NER. Sufficient for detecting names and addresses. The large model (`en_core_web_lg`, ~500MB) is unnecessary for this scope.

---

## Architectural decisions

### Framework decision: self-implemented retrieval + LangSmith

The prototype implements retrieval directly (pgvector queries + rank-bm25 + score fusion + cross-encoder re-ranking) rather than using LangChain or LlamaIndex as an orchestration framework.

**Why:** The retrieval patterns in this system are straightforward — vector similarity search, keyword search, score fusion, metadata filtering, and re-ranking. These operations are well-served by direct database queries and Python functions. A framework adds an abstraction layer over these operations without improving retrieval quality, while introducing a large dependency tree with frequent breaking changes across versions.

Building the retrieval layer directly demonstrates understanding of RAG concepts at the implementation level — how embeddings map to similarity scores, how BM25 term frequency interacts with vector relevance, how Reciprocal Rank Fusion weights are tuned. This is the knowledge that matters in interviews and production debugging, not familiarity with a framework's API.

**What we use instead:** FastAPI + psycopg2 (with pgvector) + rank-bm25 + Anthropic SDK + custom retrieval functions. The "object type registry" pattern is a Python configuration dict, not a framework feature.

**LangSmith for observability:** LangSmith works with any Python code, not just LangChain. It provides query tracing and retrieval inspection — the ecosystem-relevant tooling without coupling retrieval logic to a framework.

**Future flexibility:** LangChain can be introduced surgically for specific components if complexity warrants it. For example, the self-querying retriever pattern (where the LLM extracts structured constraints from the query) may be worth using LangChain for when Professionals and Spaces are added in production Phase 3. This would be a targeted addition, not a full framework adoption.

**Interview framing:** "I evaluated LangChain and LlamaIndex and chose self-implementation with LangSmith for observability. The retrieval patterns were straightforward enough that a framework would add abstraction without improving quality. I wanted to understand the retrieval concepts at the implementation level — term frequency, score fusion, re-ranking — rather than delegating them to a framework. LangSmith gives me the tracing and debugging tooling without the coupling."

### Synthesis model: API vs self-hosted

The prototype uses the Anthropic API (Claude Haiku 3.5) for query synthesis. The architecture supports swapping this for a self-hosted model (e.g., Llama 3 via Ollama) as a configuration change, not an architectural one.

**Why API for the prototype:** A self-hosted synthesis model would require a significantly larger VPS — GPU-capable instances or 16-48GB RAM depending on model size and quantization. Response quality for citation-heavy, nuanced synthesis would be noticeably lower than Haiku. The cost tradeoff doesn't favor self-hosting at prototype volumes.

**Why the architecture supports self-hosting:** The retrieval pipeline, score fusion, re-ranking, prompt construction, and response formatting are all model-agnostic. The model call is a single function that takes a prompt and returns text. Changing the provider means changing that function, not the pipeline.

**When self-hosting makes sense:** Air-gapped or offline environments (defense, classified systems, on-premise healthcare with strict data residency requirements). This is a real and growing deployment pattern. The prototype's architecture accommodates it without structural changes — only the model call and the VPS sizing change.

**Privacy on the API path:** PII pre-processing (Presidio) strips or generalizes identifiable information before any query reaches the Anthropic API. Anthropic also offers data processing agreements for privacy-sensitive use cases. The production spec adds Vertex AI routing (within GCP's infrastructure) as an additional layer.

### BM25 production migration path

The prototype uses `rank-bm25`, a Python library that builds a BM25 keyword index in memory. In production, keyword search migrates to Postgres full-text search (`tsvector`/`tsquery`).

**Why rank-bm25 for the prototype:** Working with a standalone BM25 implementation provides direct experience with the algorithm — term frequency, inverse document frequency, score normalization, and how keyword relevance interacts with vector similarity in the fusion step. This understanding directly informs how Postgres FTS field weights are configured in production.

**Why Postgres FTS for production:** An in-memory BM25 index has three production concerns:
1. **Cold start cost.** Cloud Run instances scaling to zero must rebuild the index on every cold start. At thousands of records, this adds meaningful startup latency.
2. **Multi-instance inconsistency.** Multiple Cloud Run instances build independent indexes. If data changes between instance starts, some instances serve stale keyword results while vector results (queried from the database) are fresh.
3. **Memory pressure.** The in-memory index competes with the cross-encoder model and FastAPI for process memory in Cloud Run's tighter memory limits.

**What carries forward unchanged:** The score fusion logic (Reciprocal Rank Fusion, weight tuning between vector and keyword relevance), the re-ranking step, and everything downstream of retrieval. The migration changes where keyword search runs, not how hybrid retrieval works.

**No functionality loss:** BM25 and Postgres FTS solve the same problem with equivalent algorithms. For short structured documents (Practice records, resource chunks), the two produce very similar rankings. The tunable parameters differ in form (BM25's `k1`/`b` vs Postgres FTS weight classes A/B/C/D) but serve the same purpose.

---

## Query flow

A query passes through the following steps:

1. **Query arrives** at the FastAPI `/query` endpoint with a query string and optional object type scope.
2. **PII pre-processing** (Presidio): strip personal names, generalize street addresses to city level, preserve search-relevant terms (city, condition, specialty). Log what entity types were redacted (not the original values).
3. **Insurance detection**: if the query mentions insurance-related terms (insurance, coverage, accepts, in-network, copay, etc.), flag the response for a prominent disclaimer (see [Insurance guardrail](#insurance-guardrail)).
4. **Embed the query** via Ollama (nomic-embed-text) to produce a vector for similarity search.
5. **Hybrid retrieval**: run pgvector similarity search and rank-bm25 keyword search in parallel against the relevant table(s).
6. **Score fusion**: combine vector similarity scores and BM25 keyword scores using Reciprocal Rank Fusion (RRF). RRF ranks each result by its position in each list and produces a merged ranking that balances semantic relevance with keyword matches.
7. **Cross-encoder re-ranking**: pass the top candidates (e.g., top 20 from fusion) through the cross-encoder model, which scores each candidate against the original query text. The cross-encoder is more accurate than the bi-encoder (embedding) similarity but too slow to run on the full corpus — hence the two-stage approach. Return the top results (e.g., top 5) after re-ranking.
8. **Build prompt**: construct the LLM prompt from retrieved context, the system prompt with guardrails, and the (PII-cleaned) user query.
9. **Synthesize** via Anthropic API (Haiku 3.5). The model generates a response constrained to the retrieved context, with citations linking to source records.
10. **Return** a structured response containing: synthesized text, citations with source links, insurance disclaimer (if flagged), and an AI-assisted label.

---

## Ingestion pipeline

Ingestion runs on the VPS via FastAPI task endpoints. The core logic lives in framework-agnostic Python modules (following the pattern established by the main platform's tasks service: `compute_sensory_profiles.py`). Each module takes a database connection and configuration, does the work, and returns a result. The FastAPI task endpoints are thin HTTP triggers.

### Project structure

```
ingestion/
  ingest_npi.py            # core: download, filter, transform, embed, upsert
  ingest_resources.py      # core: fetch, extract, chunk, embed, upsert
orchestration/
  app.py                   # FastAPI service (query-time endpoints)
  retrieval.py             # hybrid retrieval, fusion, re-ranking
  registry.py              # object type registry config
tasks/
  router.py                # FastAPI router for /tasks/ endpoints
```

### NPI ingestion

1. Download the NPI full replacement file (CSV, updated monthly by CMS).
2. Filter rows by taxonomy code prefix and San Francisco County.
3. Transform each matching row into a Practice-shaped document using the field mappings (see [Data model](#data-model)). Write records to a local JSON file for inspection before embedding.
4. Generate embeddings by concatenating relevant text fields (practice name, service descriptions, neighborhood, city, state, professional names and credentials) into a single string per record, with the neighborhood embedded in natural language (see [Neighborhood enrichment](#neighborhood-enrichment)).
5. Upsert into the practices table in the vector store with structured metadata columns (city, state, ZIP, taxonomy code, services) stored separately for filtering.
6. The ingestion script is idempotent — re-running on already-ingested data produces no duplicate records (upsert on NPI number as the primary key).

### Neighborhood enrichment

San Francisco users are likely to search for practices using neighborhood names — the Sunset, SoMa, Inner Richmond, Hayes Valley — rather than street addresses. NPI records only include street addresses, so neither BM25 nor vector search would reliably match a query like "occupational therapist in the Sunset" to a practice at 1234 Irving St.

This is handled at ingestion time, not query time. During NPI ingestion, each practice's street address is reverse geocoded to determine its SF neighborhood. The neighborhood name is then included directly in the text that gets embedded. For example, instead of embedding "1234 Irving St, San Francisco, CA 94122", the embedded text reads "located in the Sunset district of San Francisco (1234 Irving St, 94122)". This makes neighborhood names a natural part of the corpus, so both BM25 keyword matching and vector similarity search handle neighborhood queries without any special routing or conditional logic at query time.

**Geocoding source:** San Francisco's official neighborhood boundary dataset (from DataSF), which maps coordinates to named neighborhood districts. The boundary polygons are loaded once at ingestion time and used for point-in-polygon lookups — no external API calls per record.

**Neighborhood is also stored as a metadata column** on the practices table, enabling direct SQL filtering for neighborhood-scoped queries (e.g., a query mentioning "Sunset" can filter `WHERE neighborhood = 'Sunset'` in addition to semantic matching). This parallels the existing metadata filtering for ZIP codes and service types.

**County expansion note:** This enrichment is SF-specific. When expanding to other counties, the same pattern applies if the county has an official neighborhood/district boundary dataset. Counties without such data skip this step — the system still works, just without neighborhood-level matching.

### Resource ingestion

1. Read the curated URL list from a CSV or JSON file.
2. For each URL, attempt extraction with Trafilatura (HTTP fetch + article body extraction to clean markdown).
3. If Trafilatura returns empty content (likely a JavaScript-rendered page), fall back to Playwright (headless browser) to render the page, then extract with Trafilatura from the rendered HTML.
4. Log any pages where both methods fail for manual review.
5. Store extracted markdown as a local file keyed by a slugified version of the URL.
6. Apply chunking: semantic chunking with contextual headers, targeting 300-600 tokens per chunk, with the section heading prepended to each chunk. (A "chunk" is a piece of a longer document. Documents are split into chunks because embedding models and LLMs work better with focused, coherent passages than with entire long documents.)
7. Generate embeddings per chunk via Ollama.
8. Upsert into the resource_chunks table with source metadata (URL, organization name, page title, fetch date, county scope if applicable) attached to each chunk.
9. Idempotent — re-running on already-ingested URLs produces no duplicate chunks (upsert on content hash).

### Embedding model versioning

The embedding model version (`nomic-embed-text` version string) is stored as metadata alongside every vector in the database. If the embedding model is upgraded, all existing vectors must be re-embedded — mixing vectors from different model versions produces incorrect similarity results. At the prototype's data volume, a full re-embed is a minutes-long batch job triggered via `/tasks/re-embed`.

---

## Hybrid retrieval

Hybrid retrieval combines two complementary search methods to improve result quality over either method alone.

### Vector similarity search (pgvector)

Vector search finds results that are semantically similar to the query, even if they don't share exact words. The query is embedded into a vector (a list of numbers representing its meaning), and pgvector finds stored vectors that are closest in the embedding space. This handles queries like "help for my kid who has trouble focusing in class" matching content about ADHD accommodations, even though the query never uses the term "ADHD."

pgvector uses an HNSW (Hierarchical Navigable Small World) index for fast approximate nearest neighbor search. One HNSW index per object type table.

### Keyword search (rank-bm25)

Keyword search finds results containing the specific terms in the query. BM25 (Best Matching 25) scores documents based on how often query terms appear, weighted by how rare each term is across the corpus and normalized by document length. This handles queries using specific terminology — "LMFT in 94110" — where the exact term matters more than semantic similarity.

The BM25 index is built in memory when the FastAPI service starts. It is rebuilt via the `/tasks/rebuild-bm25-index` endpoint after new data is ingested.

### Reciprocal Rank Fusion (RRF)

Vector search and keyword search return separate ranked lists. RRF merges them by scoring each result based on its rank position in each list: `score = 1 / (k + rank)` where `k` is a constant (typically 60). A result that appears high in both lists gets a high fused score. A result that appears high in one list but not the other still gets credit. This produces a merged ranking that balances semantic relevance with keyword precision.

### Cross-encoder re-ranking

The initial retrieval (vector + BM25 + fusion) returns a broad candidate set (e.g., top 20). A cross-encoder model then re-ranks these candidates. Unlike the bi-encoder used for embeddings (which embeds the query and document separately and compares vectors), a cross-encoder processes the query and document together, producing a more accurate relevance score at the cost of being slower. This two-stage approach — fast broad retrieval, then accurate re-ranking on a small set — is a standard RAG pattern.

The prototype uses `cross-encoder/ms-marco-MiniLM-L-6-v2` from the `sentence-transformers` library. It is ~90MB loaded in memory and runs on CPU without GPU.

### Metadata filtering

For Practice queries, structured constraints are extracted from the query using pattern matching and keyword detection (e.g., ZIP code regex, neighborhood name lookup, service/specialty term lookup against the known taxonomy). These constraints are applied as SQL WHERE clauses alongside the vector similarity search. For example, a query mentioning "occupational therapist" filters by the relevant service/taxonomy code, a query mentioning "94110" filters by ZIP code, and a query mentioning "the Sunset" filters by neighborhood. This narrows the candidate set to relevant records before semantic ranking.

In production Phase 3 (Professionals and Spaces), this extraction may benefit from an LLM-based self-querying approach where the model parses complex multi-constraint queries. For the prototype's scope (single county, straightforward query patterns), pattern matching is sufficient and avoids an additional LLM call per query.

---

## Guardrails

### Hallucination prevention

- The system prompt explicitly constrains the model to use only the retrieved context — not pre-training knowledge. This is the most important guardrail.
- Every claim in the synthesized response must cite a specific source record, with a link back to the source (provider directory entry or resource URL).
- When retrieval returns weak results (low similarity scores, few matches), the system returns a low-confidence fallback response: "I couldn't find relevant results for your query. Please try rephrasing, or use the standard directory search to browse providers and resources directly." The system never speculates or fills gaps with pre-training knowledge.

### Insurance guardrail

The system never states that a specific provider accepts a specific insurance plan.

**Why:** Insurance coverage information is always outdated. Published provider directories, insurance company websites, and third-party databases routinely contain stale insurance data. A provider who accepted Blue Shield last month may not accept it today. This was confirmed by a contact working in a healthcare provider's office — the only reliable way to verify insurance coverage is to call the provider directly. For the NEC's user population (people who may be under significant cognitive load and trusting of the system's answers), presenting outdated insurance information as fact could lead to real harm: a parent drives across the city to a provider who doesn't actually take their insurance.

**Implementation:**

1. **System prompt instruction:** The model is explicitly told to never claim a provider accepts or does not accept any specific insurance plan. If the query mentions insurance, the model must include the disclaimer.
2. **Query-level detection:** Before synthesis, the query is checked for insurance-related terms (insurance, coverage, accepts, in-network, out-of-network, copay, deductible, HMO, PPO, Medi-Cal, Medicare). If detected, the response is flagged to include a prominent disclaimer — not as a footnote, but as a primary part of the answer: "Insurance coverage changes frequently and cannot be verified through this search. Please contact the provider directly to confirm they accept your plan."

### AI transparency

All responses are clearly labeled as AI-assisted. The UI displays a persistent notice: "These results are generated by AI from indexed data and may contain errors. Please verify information directly with providers and organizations."

---

## PII pre-processing

Before any query is sent to the Anthropic API, Microsoft Presidio processes the query to generalize or strip identifiable personal information. This is a full implementation, not a stub — real users and recruiters will interact with the prototype.

**What is stripped:**
- Personal names (detected via spaCy NER)

**What is generalized:**
- Street addresses are reduced to city level ("123 Oak Street San Francisco" → "San Francisco")

**What is preserved intact:**
- City/region, insurance carrier names (for the insurance guardrail to detect), condition names, specialty terms, and all other search-relevant terms

**Logging:** Each PII pre-processing step logs what entity types were redacted and how many (e.g., "1 PERSON entity stripped, 1 ADDRESS entity generalized"), but never logs the original values. This logging supports quality monitoring — if the PII step is aggressively stripping search-relevant terms, the logs will show high redaction rates correlating with poor retrieval results.

**Library:** Presidio Analyzer with spaCy `en_core_web_md` as the NER backend. Expected latency overhead: under 50ms per query.

---

## Abuse protection

The Anthropic API introduces a variable cost that scales with query volume. Three layers of protection prevent abuse and cap financial exposure.

### Layer 1: Cloudflare

The prototype's domain is routed through Cloudflare (free tier). This provides DDoS protection, bot detection, and the ability to enable "Under Attack" mode if needed. Bot traffic is blocked before it reaches the FastAPI service.

### Layer 2: Application rate limiting

FastAPI rate limiting via `slowapi` (built on the `limits` library):

- **Per-IP limits:** 10 queries/minute, 100 queries/hour per IP address
- **Global daily budget:** Hard cap on total Anthropic API calls per day (starting value to be determined during testing). Once hit, the query endpoint returns: "Natural language search has reached its daily limit. Please try again tomorrow, or use the standard directory search." This is the financial circuit breaker.

### Layer 3: Anthropic spending limit

The Anthropic API dashboard allows setting a monthly spending limit on the API key. Set to **$25/month** as a starting value. If the limit is reached, the API rejects requests. This is the last-resort backstop — even if application rate limiting fails and a flood gets through, the monthly bill is capped.

At Haiku 3.5 pricing (~$0.005 per query), $25/month allows approximately 5,000 queries — well above prototype needs, with headroom for eval harness runs.

---

## Evaluation

Given that the intended users may be in distress when querying, retrieval failures are not just a quality issue — they can mean someone doesn't find help they need. A meaningful evaluation framework is part of the prototype scope, not deferred.

### Golden test suite

**Start with 30 baseline queries:**
- ~15 practice/provider queries: "find an OT in the Sunset District," "therapist specializing in autism near the Mission," "psychiatrist in San Francisco who does remote sessions"
- ~15 resource/informational queries: "what is an IEP," "how to prepare for a therapy evaluation in SF," "sensory accommodations in San Francisco public schools"
- ~5 edge cases: queries with no relevant results (should trigger the low-confidence fallback), ambiguous queries that span both object types, queries with PII that Presidio should catch

**Expand to 50-100 queries based on observed failure modes.** After the baseline queries are scored, use the failures to generate targeted probes:

- If the system struggles with multi-constraint practice queries ("autism specialist in San Francisco who does remote sessions"), write 10 more variations targeting that weakness with different constraint combinations.
- If it hallucinates credentials that aren't in the source data, write queries that specifically probe for credential claims across different provider types.
- If resource retrieval returns irrelevant chunks for specific topic areas, write queries that stress-test those topics with different phrasings.

This failure-mode-driven approach produces a test suite that targets the system's actual weak points rather than covering a predetermined grid.

### Scoring modes

Both scoring modes are built into the eval harness from the start:

- **Exact match** for factual lookups where the correct answer is deterministic: "find an OT in San Francisco" — did the response include at least one occupational therapist with a San Francisco address? Is the NPI number correct?
- **LLM-as-judge** for open-ended synthesis where correctness is a matter of quality: "what should I know before my child's first therapy appointment?" — have a judge model evaluate whether the response is accurate, relevant, and appropriately cautious.

### RAGAS metrics

RAGAS (ragas.io) provides automated evaluation metrics:

- **Context recall:** Did the retrieved records contain the information needed to answer the query?
- **Faithfulness:** Did the synthesized response stay within the retrieved context, or did it introduce claims not supported by the retrieved records?
- **Answer relevance:** Did the response actually address what the user asked?

Baseline RAGAS scores are established on the initial 30-query suite before any tuning. Scores are re-run after any change to the retrieval configuration, chunking strategy, system prompt, or re-ranking parameters.

### User feedback

A binary "was this helpful?" mechanism in the frontend. Responses logged as positive or negative. Negative-feedback queries are promoted into the golden test suite for investigation. At prototype scale this is a manual review process — periodically review negative feedback and use it to identify retrieval or prompt issues.

### Latency targets

Track p50 and p99 latency separately for retrieval and generation:

- **Retrieval:** under 200ms p99. If high, investigate HNSW index parameters before touching other layers.
- **Total response:** under 3 seconds p99. Generation latency from the Anthropic API is expected to dominate; this is acceptable at prototype scale.

---

## Observability

### LangSmith

LangSmith (free tier) is configured from the start for query tracing and retrieval inspection. It traces:

- The full retrieval call: query text, filters applied, records returned with similarity scores
- The prompt sent to the LLM
- The synthesized response
- Latency breakdown per step

LangSmith works with any Python code via its tracing SDK — it does not require LangChain.

### Structured logging

Every query produces a structured log entry containing:

- Timestamp
- Anonymized query text (post-PII-processing)
- Object type scope
- Retrieval method used (vector / keyword / hybrid)
- Retrieval latency (ms)
- Generation latency (ms)
- Whether the low-confidence fallback fired
- Whether the insurance disclaimer was triggered
- Retrieved chunks with similarity scores
- Per-chunk flag for whether each chunk was cited in the final response

The gap between chunks retrieved and chunks cited is a diagnostic signal. If the model consistently ignores high-scoring chunks, that indicates a prompt or context-window issue, not a retrieval one.

Log format is JSON, compatible with LangSmith import for combined analysis.

### Where model observability lives

The cross-encoder (sentence-transformers) and PII models (Presidio + spaCy) are Python libraries loaded directly into the FastAPI process — they are not separate services. Their errors, timing, and debug output are part of FastAPI's structured logs. In production, these appear in Dozzle alongside all other FastAPI container logs.

Ollama runs as a system service on the VPS, outside Docker. Its logs are accessible via `journalctl -u ollama` but are rarely needed — Ollama is a stateless embedding service that receives requests and returns vectors. The meaningful observability for embeddings (which queries were embedded, latency, dimension validation) is captured by the FastAPI code that calls Ollama, not by Ollama itself.

LangSmith provides the high-level model observability: full query pipeline traces showing what was retrieved, similarity scores, the prompt sent to the synthesis model, and the response returned. This is the primary tool for understanding model behavior — structured logs capture the operational data, LangSmith captures the semantic data.

### Production observability migration

The prototype uses LangSmith + structured JSON logs to stdout (captured by Docker). In production on GCP, this migrates to Fluent Bit + OpenTelemetry → Google Cloud Logging, providing centralized log aggregation, trace correlation, and alerting. The structured JSON format is designed to be compatible with both approaches — the migration changes where logs are shipped, not how they are produced. This is deferred to the production migration phase.

---

## Frontend

A standalone Next.js application serving as a demo client. It is clearly decoupled from the FastAPI service — it calls the same API that the main NEC platform will eventually call. The frontend has no special access or privileged endpoints.

### Features

- Text input for natural language queries
- Response area rendering synthesized text with linked citations (source URLs for resources, directory links for providers)
- Prominent insurance disclaimer when triggered
- Persistent "AI-assisted results" label
- Binary "was this helpful?" feedback buttons
- Collapsible debug panel showing: retrieved chunks with similarity scores, retrieval latency, generation latency, PII redaction summary
- Offline fallback message if the FastAPI service is unavailable

### Deployment

Deployed as a separate container in the Dokploy stack. Styled with TailwindCSS. Domain routed through Cloudflare.

---

## Infrastructure

### VPS

**Hetzner CX33** — 4 Intel vCPU, 8GB RAM, 80GB SSD, $8.59/month (cost-optimized shared vCPU).

Memory budget:

| Component | Estimated RAM |
|-----------|--------------|
| Postgres + pgvector | 500MB – 1GB |
| Ollama + nomic-embed-text | 400 – 600MB |
| FastAPI + cross-encoder model | 300 – 400MB |
| Presidio + spaCy (en_core_web_md) | 150 – 250MB |
| BM25 index (SF county, few thousand docs) | ~50MB |
| OS + overhead | ~500MB |
| **Total** | **1.9 – 2.8GB** |

Leaves 5-6GB headroom for running ingestion scripts or the eval harness alongside the service. If memory becomes a constraint during development, upgrade in-place to **Hetzner CX43** (8 vCPU, 16GB RAM, 160GB SSD, $14.59/month).

**Why Intel over Ampere (ARM):** Some Python packages with native C extensions (sentence-transformers, psycopg2-binary, numpy) can have ARM build issues. Intel avoids platform compatibility friction during development.

### Dokploy

Separate Dokploy instance on the prototype's VPS, completely isolated from the main NEC platform's Dokploy/VPS. Manages the Docker Compose stack (FastAPI, Postgres, Ollama, Next.js).

### Cloudflare

Free tier. DNS routing, DDoS protection, bot detection. Consistent with the main NEC platform's Cloudflare usage.

---

## Implementation order

Each step is scoped to be a self-contained unit that can be broken down into a detailed implementation plan with tests and commits at clean boundaries.

### Step 1 — Environment and vector store setup

Set up the Python project repository. Provision the Hetzner CX33 VPS and configure Dokploy. Start Postgres + pgvector via Docker Compose using the official `pgvector/pgvector` image. Define table schemas (practices, resource_chunks) including metadata columns and the embedding vector column. Install and verify Ollama with `nomic-embed-text` on the VPS. Confirm end-to-end: generate a test embedding and write it to the vector store.

### Step 2 — NPI ingestion

Download the NPI full replacement file. Write the filter and transform script: filter by taxonomy code and San Francisco County, transform into Practice-shaped documents, write a local JSON file for inspection. Implement embedding generation. Upsert into the practices table with metadata columns stored separately for filtering. Verify record count and spot-check a sample of records.

### Step 3 — Resource ingestion

Compile the curated URL list (50-100 pages). Write the fetch-and-extract script using Trafilatura with Playwright fallback. Implement chunking (semantic chunking with contextual headers, 300-600 tokens per chunk). Generate embeddings per chunk. Upsert into the resource_chunks table with source metadata. Log extraction failures for manual review. Verify chunk count and spot-check retrieval on test queries.

### Step 4 — Orchestration service skeleton + Anthropic API + LangSmith

Implement the FastAPI service with the query request/response contract and the `/tasks/` router for ingestion endpoints. Wire the Anthropic SDK (Haiku 3.5). Add LangSmith tracing. Add the health check endpoint. Add per-IP rate limiting (slowapi) and basic prompt injection detection.

### Step 5 — Hybrid retrieval + BM25 + cross-encoder re-ranking

Implement vector similarity search via pgvector. Implement keyword search via rank-bm25. Implement Reciprocal Rank Fusion for score merging. Add the cross-encoder re-ranking step. Implement metadata filtering for Practice queries (extract structured constraints from the query). Tune retrieval against a small set of test queries before proceeding.

### Step 6 — PII pre-processing

Integrate Presidio into the query pipeline. Configure entity detection for names and addresses. Implement generalization logic (strip names, city-level address normalization). Add redaction logging. Validate against test queries including edge cases (no PII, partial PII, PII-heavy queries).

### Step 7 — Guardrails

Implement the system prompt with hallucination prevention guardrails. Implement the low-confidence fallback. Implement the insurance detection and disclaimer. Test guardrails explicitly: send queries where no relevant records exist (confirm fallback fires), send queries mentioning insurance (confirm disclaimer appears), send queries that tempt the model to use pre-training knowledge (confirm it stays within retrieved context).

### Step 8 — Eval harness

Build the evaluation harness with RAGAS integration. Create the initial 30-query golden test suite. Implement both scoring modes (exact match + LLM-as-judge). Run baseline evaluation and record scores. Expand the test suite to 50-100 queries based on observed failure modes. Add the user feedback mechanism (binary helpful/not-helpful logging).

### Step 9 — Frontend

Build the Next.js demo client. Implement the query input, response rendering with citations, insurance disclaimer display, AI-assisted label, feedback buttons, and debug panel. Implement the offline fallback. Deploy as a separate Dokploy container. Configure Cloudflare routing.

---

## Cost estimate

| Item | Monthly cost |
|------|-------------|
| Hetzner CX33 VPS | $8.59 |
| Anthropic API (Haiku 3.5, prototype volumes) | $1 – 5 |
| Cloudflare (free tier) | $0 |
| LangSmith (free tier) | $0 |
| **Total** | **~$10 – 14/mo** |

At Haiku 3.5 pricing (~$0.005 per query), the $25/month Anthropic spending cap allows approximately 5,000 queries/month.

**Scaling note:** If the prototype sees higher traffic (e.g., attention from a blog post), costs scale linearly with queries. At 50 queries/day (~1,500/month), Anthropic costs would be ~$7.50/month — still well within the spending cap. The model upgrade path (Haiku 3.5 → Haiku 4 → Sonnet) increases per-query cost but also increases response quality.

---

## County expansion playbook

Adding a new county to the system is a repeatable process, consistent with the nonprofit's county-by-county scaling model:

1. **NPI data:** Change the county filter in the NPI ingestion script. Run `/tasks/ingest-npi`. Provider records for the new county are added to the existing practices table. Automated, minutes of work.
2. **Curate county-specific resources:** Identify local organizations, school district guides, county behavioral health resources, and local support groups. Compile the URL list. This is the manual, time-intensive step — expect hours to days per county depending on how much local content exists.
3. **Ingest resources:** Add the new URLs to the resource list. Run `/tasks/ingest-resources`. New resources are embedded and added to the vector store immediately.
4. **Verify retrieval quality:** Run the eval harness with queries targeting the new county. Add county-specific queries to the golden test suite.
5. **Rebuild BM25 index:** Run `/tasks/rebuild-bm25-index` to include the new records in keyword search.

National resources (already in the vector store) are immediately available to users querying from the new county. County-specific resources provide the localized relevance.

The expansion process itself is a documentable deliverable and a candidate for the blog series.

---

## Resource schema learnings

The prototype ingests the curated resource corpus with a minimal schema: title, source URL, organization name, content (markdown), fetch date, and county scope (national / SF-specific). During ingestion and retrieval tuning, observations about what metadata is useful are documented as a concrete prototype output.

Questions to resolve during prototype work:

- **`description` field:** For externally ingested resources, should this be the page's meta description tag, the first paragraph of body content, or a generated summary? The answer affects both display quality and retrieval quality.
- **`resourceType` enum:** Do values like `article`, `guide`, `factsheet`, `tool` cover the content types encountered in real curated content?
- **Topic vocabulary:** Tagging 50-100 resources against a draft vocabulary will surface gaps and over-broad categories.
- **Scope/geography:** How should county vs. state vs. national scope be represented? How is scope inferred from content (e.g., a resource mentioning "California IEP law" is California-scoped)?

These observations are taken to the main NEC megadirectory codebase and implemented as a Django Resource model in a separate task. The SRIP Resource schema is a later concern that benefits from the same learnings.

---

## Blog series structure

This project is structured as a publishable tutorial series on building a real-world RAG system with hybrid retrieval, evaluation, and re-ranking.

| Post | Topic | Key concepts covered |
|------|-------|---------------------|
| 1 | Architecture and motivation | The domain problem; design decisions (LangChain evaluation, self-hosted vs API synthesis); the object type registry pattern |
| 2 | Ingestion pipeline | NPI public data, web scraping with Trafilatura + Playwright, chunking strategies, self-hosted embeddings with Ollama |
| 3 | Hybrid retrieval | pgvector similarity search, BM25 keyword search, Reciprocal Rank Fusion, cross-encoder re-ranking |
| 4 | Guardrails and PII | Presidio PII pre-processing, hallucination prevention, domain-specific guardrails (the insurance story) |
| 5 | Evaluation | RAGAS metrics, golden test suites, failure-mode-driven testing, LLM-as-judge vs exact match |
| 6 | Deployment and cost control | Dokploy on Hetzner, Cloudflare, rate limiting, Anthropic spending caps, county expansion |

The repository is offered whole, with tagged commits or branches corresponding to each post's end state. Readers can follow along incrementally or clone the finished product.
