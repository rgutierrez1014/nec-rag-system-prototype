# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered natural language search service for the Neurodivergent Equity Coalition (NEC) platform. A standalone RAG (Retrieval-Augmented Generation) prototype scoped to San Francisco County, deployed as a FastAPI API with a Next.js demo frontend. The API is the product — the frontend is a demo client.

This is not a throwaway prototype. The architecture, retrieval pipeline, ingestion transforms, eval harness, and guardrails are designed to carry forward into production.

## Architecture

```
FastAPI Service (query + ingestion tasks)
  ├── /query → PII preprocessing → hybrid retrieval → rerank → Anthropic API (Haiku 3.5) → response + citations
  ├── /tasks/ingest-npi
  ├── /tasks/ingest-resources
  ├── /tasks/rebuild-bm25-index
  └── /tasks/re-embed

Postgres + pgvector (vectors, metadata, full-text)
Ollama + nomic-embed-text (self-hosted embeddings)
Next.js Frontend (demo client, separate container)
```

All containers managed by Dokploy on a Hetzner CX33 VPS behind Cloudflare.

### Key Design Decisions

- **No LangChain/LlamaIndex** — retrieval is self-implemented (pgvector + rank-bm25 + RRF score fusion + cross-encoder reranking). LangSmith is used for observability without framework coupling.
- **Practice-centric data model** — the primary searchable unit is a Practice (not a Professional), mirroring the main NEC platform's data model for production portability.
- **Object type registry** — declarative config dict per searchable type (Practices, Resources). Adding a new type means adding a config entry and ingestion transform, not modifying retrieval logic.
- **BM25 in-memory for prototype** — production migrates to Postgres FTS (`tsvector`/`tsquery`). Score fusion and everything downstream is unchanged.

## Project Structure

```
ingestion/
  ingest_npi.py            # download, filter, transform NPI data → Practice documents → embed → upsert
  ingest_resources.py      # fetch URLs (Trafilatura + Playwright fallback), chunk, embed, upsert
orchestration/
  app.py                   # FastAPI service (query endpoints)
  retrieval.py             # hybrid retrieval, RRF fusion, cross-encoder reranking
  registry.py              # object type registry config
tasks/
  router.py                # FastAPI router for /tasks/ endpoints
```

### Ingestion Pattern

Core functions are framework-agnostic: they take a DB connection and config, do the work, return a result. FastAPI task endpoints are thin HTTP triggers. This follows the pattern from the main NEC platform's tasks service (`compute_sensory_profiles.py`). Use `psycopg2` with `execute_values()` for bulk upserts — no ORM.

## Tech Stack

| Component | Choice |
|-----------|--------|
| API | FastAPI + direct Anthropic SDK |
| Vector store | Postgres + pgvector |
| Keyword search | rank-bm25 (prototype), Postgres FTS (production) |
| Embeddings | Ollama + nomic-embed-text (self-hosted) |
| Synthesis | Claude Haiku 3.5 via Anthropic API |
| Reranking | sentence-transformers + cross-encoder/ms-marco-MiniLM-L-6-v2 (~90MB, CPU-only) |
| PII | Microsoft Presidio + spaCy en_core_web_md |
| Observability | LangSmith (free tier) |
| Evaluation | RAGAS |
| Frontend | Next.js + TailwindCSS |
| Infra | Hetzner CX33 + Dokploy + Cloudflare |

**PyTorch:** CPU-only build (`pip install torch --index-url https://download.pytorch.org/whl/cpu`). No GPU on the VPS.

## Domain Rules

These are non-negotiable content and guardrail policies:

- **Source exclusions:** Nothing from autismspeaks.org. Nothing from ABA-affiliated sources.
- **Insurance guardrail:** Never state that a provider accepts a specific insurance plan. Always direct users to contact providers directly. If a query mentions insurance terms, flag the response for a prominent disclaimer.
- **Hallucination prevention:** The synthesis model must use only retrieved context, never pre-training knowledge. Every claim must cite a source. Weak retrieval triggers a low-confidence fallback, never speculation.
- **AI transparency:** All responses labeled as AI-assisted.
- **PII:** Presidio strips personal names, generalizes street addresses to city level before any query hits the Anthropic API. Log entity types redacted (never original values).

## Data Model

**Practice documents** (from NPI registry): name, description, practice_type, address, services (mapped from NPI taxonomy codes), specialties, presence_types, professional roster, NPI number.

**Resource chunks** (from curated URLs): source URL, org name, page title, content chunk, fetch date, county scope. Chunked at 300-600 tokens with contextual headers. Keyed by content hash for idempotent upserts.

Services and specialties are stored as slug arrays (e.g., `["occupational-therapy"]`) matching the main platform's `Service.name_slug` values. Service filtering must respect the MPTT hierarchy — a query for "therapy" should match all descendant services.

## Query Pipeline

1. PII preprocessing (Presidio)
2. Insurance term detection → flag for disclaimer
3. Embed query via Ollama
4. Hybrid retrieval: pgvector similarity + BM25 keyword search (parallel)
5. Reciprocal Rank Fusion (RRF) score merging
6. Cross-encoder reranking (top ~20 → top ~5)
7. Metadata filtering for Practices (ZIP regex, service/specialty term lookup)
8. Build prompt with guardrails system prompt
9. Synthesize via Anthropic API (Haiku 3.5)
10. Return structured response with citations and disclaimers

## Local Development

**Infrastructure:** Postgres + pgvector and Ollama run permanently on the Hetzner VPS. Local dev connects via SSH tunnel. The tunnel makes VPS ports appear local (`localhost:5432`, `localhost:11434`).

**First-time setup:**
```bash
make setup-api   # create venv, install dependencies
make tunnel      # open SSH tunnel
make setup-db              # create + migrate nec_rag_dev (default)
make setup-db db=nec_rag   # create + migrate nec_rag (production)
```

**Daily workflow:**
```bash
make start-dev   # tunnel + FastAPI on :8000
make stop-dev    # shut everything down
make verify      # end-to-end Ollama → pgvector check (requires tunnel)
make test        # integration tests — creates/drops nec_rag_test automatically (requires tunnel)
make test TEST_ARGS="test/test_chunking.py -v"  # pass args directly to pytest, run a single test, etc.
```

**Three databases on one VPS Postgres instance:**
- `nec_rag` — production (deployed container)
- `nec_rag_dev` — local development (`POSTGRES_DB` in `.env`)
- `nec_rag_test` — test suite only; pytest creates and drops it each run

**Schema changes:** Add a numbered file to `api/db/migrations/` (e.g., `0002_*.sql`). Never edit `0001_initial.sql`. Both `setup-db` and `apply-migrations` accept an optional `db=<name>` argument (default: `nec_rag_dev`). Run against each database explicitly — there is no "apply to all" shortcut.

**pgvector query gotcha:** psycopg2 sends Python lists as `numeric[]`, not `vector`. Always cast embedding parameters explicitly: `%s::vector`. This applies to all `<=>` similarity queries.

## Reference Documents

- `docs/SPEC.md` — full prototype specification (architecture, data model, query flow, ingestion, guardrails, eval, infrastructure, implementation order)
- `docs/internal/nec_platform_reference.md` — main NEC platform data model, API structure, filtering patterns, and production integration surface
