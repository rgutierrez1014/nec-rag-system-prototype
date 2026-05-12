# Resource Ingestion

## Goal

Build the ingestion pipeline for curated informational resources — high-quality web pages from community-trusted organizations. The pipeline reads a curated URL list, fetches and extracts clean text from each page, chunks the content into embedding-sized passages, generates vector embeddings, and upserts everything into the `resource_chunks` table. This gives the RAG system its second searchable object type (alongside Practices from NPI data), enabling informational queries like "what is an IEP?" or "how to prepare for a therapy evaluation in SF."

## Architectural Context

**Tech stack relevant to this step:**
- **Content extraction:** Trafilatura (HTTP fetch + article body extraction) with Playwright (headless browser) as fallback for JS-rendered pages
- **Embeddings:** Ollama + `nomic-embed-text` (self-hosted, 768-dimensional vectors) — same model and `generate_embedding()` function used by NPI ingestion
- **Storage:** Postgres + pgvector (`resource_chunks` table, already migrated)
- **DB access:** `psycopg2` with `execute_values()` for bulk upserts — no ORM

**Ingestion pattern:** Core ingestion functions are framework-agnostic Python modules. They take a DB connection and config, do the work, return a result. The FastAPI `/tasks/ingest-resources` endpoint is a thin HTTP trigger added in Step 4 — this step builds the core module only.

**Object type registry:** Each searchable type (Practices, Resources) is defined as a declarative config entry. Resource ingestion establishes the second entry in this registry.

**Idempotency:** Re-running ingestion on already-ingested URLs must produce no duplicate chunks. The `resource_chunks` table uses `content_hash` (SHA-256 of chunk content) as a unique key for upsert.

**Source exclusions (non-negotiable):** Nothing from autismspeaks.org. Nothing from ABA-affiliated sources. These exclusions apply to the curated URL list.

## Prerequisites / Prior Steps

**Step 1 — Environment and vector store setup:**
- ✅ Postgres + pgvector running on VPS, accessible via SSH tunnel
- ✅ `resource_chunks` table created with schema: `content_hash`, `source_url`, `org_name`, `page_title`, `county_scope`, `fetch_date`, `content`, `chunk_index`, `section_header`, `embedding vector(768)`, `embedding_model`
- ✅ HNSW index on `resource_chunks.embedding` (cosine distance)
- ✅ Ollama + `nomic-embed-text` running on VPS
- ✅ `api/embeddings.py` with `generate_embedding()` and `get_embedding_model_version()`

**Step 2 — NPI ingestion:**
- ✅ `api/ingestion/ingest_npi.py` — establishes the ingestion pattern (download → filter → transform → embed → upsert)
- ✅ `api/ingestion/embedding.py` — batch embedding + upsert logic (may be reusable or serve as reference)

## Scope

### 1. Curated URL list

Create a seed data file (CSV or JSON) containing the curated resource URLs. Each entry includes:
- `url` — the page URL
- `org_name` — the publishing organization (e.g., "ASAN", "The Arc", "CHADD", "Understood")
- `county_scope` — `"national"` or `"san_francisco"` (determines whether the resource is geography-specific)

Start with 50-100 URLs from these trusted sources:
- autisticadvocacy.org (ASAN)
- thearc.org (The Arc)
- chadd.org (CHADD)
- understood.org (Understood)
- Other community-trusted organizations

Content types to include:
- **National foundational:** IEP rights under IDEA, therapy preparation guides, sensory accommodation overviews, diagnostic process explainers
- **San Francisco-specific:** SF Unified School District resources, county behavioral health services, local support groups, Bay Area provider guides

Content types to exclude: event listings, donation pages, staff bios, news/blog posts without substantive informational content.

### 2. Content extraction (`api/ingestion/ingest_resources.py`)

For each URL in the curated list:

1. **Primary extraction (Trafilatura):** HTTP fetch + article body extraction to clean markdown. Trafilatura handles boilerplate removal (nav, footer, ads) and outputs the main content body.
2. **Fallback extraction (Playwright):** If Trafilatura returns empty or near-empty content (likely a JS-rendered page), render the page with Playwright (headless Chromium), then pass the rendered HTML back to Trafilatura for content extraction.
3. **Failure logging:** If both methods fail for a URL, log it at WARNING level with the URL and failure reason. Do not halt ingestion — continue with remaining URLs. Collect all failures for manual review.
4. **Local storage:** Save extracted markdown to a local file keyed by a slugified version of the URL (e.g., `data/resources/asan-org-about-autism.md`). This enables inspection before embedding and avoids re-fetching on subsequent runs.

### 3. Chunking

Split extracted content into chunks suitable for embedding and retrieval:

- **Target size:** 300-600 tokens per chunk
- **Strategy:** Semantic chunking with contextual headers — split on section boundaries (headings, paragraph breaks) rather than arbitrary token counts
- **Header prepending:** Each chunk gets the most recent section heading prepended, so chunks retain context when retrieved independently (e.g., a chunk from a "Rights Under IDEA" section starts with that header even if the heading appeared paragraphs earlier)
- **Chunk metadata:** Each chunk carries its `chunk_index` (position within the source document) and `section_header`

### 4. Embedding generation

- Generate one embedding per chunk via the existing `generate_embedding()` function in `api/embeddings.py`
- Store the embedding model version alongside each vector (via `get_embedding_model_version()`)
- Batch embedding calls where possible to reduce HTTP overhead to Ollama

### 5. Database upsert

Upsert each chunk into the `resource_chunks` table:

- **Deduplication key:** `content_hash` — SHA-256 hash of the chunk content. On conflict, update the embedding and metadata (the content hasn't changed, but the embedding model version may have).
- **Fields to populate:** `content_hash`, `source_url`, `org_name`, `page_title` (extracted from the page or the curated list), `county_scope`, `fetch_date` (date of extraction), `content`, `chunk_index`, `section_header`, `embedding`, `embedding_model`
- Use `psycopg2` with `execute_values()` for bulk upserts, consistent with the NPI ingestion pattern
- **pgvector casting:** Always cast embedding parameters as `%s::vector` — psycopg2 sends Python lists as `numeric[]`, not `vector`

### 6. Reporting

After ingestion completes, log a summary:
- Total URLs processed
- Successful extractions vs. failures (with failed URLs listed)
- Total chunks generated
- Total chunks upserted (new vs. updated)

Return this summary as the function's return value (for the Step 4 task endpoint to surface via HTTP).

## Acceptance Criteria

- [ ] A curated URL list file exists with 50-100 entries from trusted sources, with no entries from autismspeaks.org or ABA-affiliated sources
- [ ] `api/ingestion/ingest_resources.py` fetches and extracts content from all URLs in the list
- [ ] Trafilatura is the primary extraction method; Playwright is used as fallback for JS-rendered pages
- [ ] Pages where both extraction methods fail are logged but do not halt ingestion
- [ ] Extracted content is saved locally as markdown files for inspection
- [ ] Content is chunked into 300-600 token passages with section headers prepended
- [ ] Each chunk is embedded via Ollama (`nomic-embed-text`) and upserted into `resource_chunks`
- [ ] Re-running ingestion on the same URLs produces no duplicate chunks (content-hash deduplication)
- [ ] Chunk count and a spot-check of sample chunks confirm reasonable extraction and chunking quality
- [ ] A test query against `resource_chunks` via pgvector similarity search returns relevant chunks

## Out of Scope

- FastAPI `/tasks/ingest-resources` endpoint (Step 4)
- BM25 keyword index for resources (Step 5)
- Hybrid retrieval, RRF fusion, cross-encoder reranking (Step 5)
- Query pipeline integration (Step 4-5)
- PII pre-processing (Step 6)
- Guardrails and system prompt (Step 7)
- Evaluation harness (Step 8)
- Frontend rendering of resource results (Step 9)
- Resource schema learnings documentation (ongoing, separate deliverable)
