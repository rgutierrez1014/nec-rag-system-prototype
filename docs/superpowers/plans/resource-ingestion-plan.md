# Resource Ingestion — Implementation Plan

## Context and Goals

Build the ingestion pipeline for curated informational resources — the RAG system's second searchable object type alongside Practices. The pipeline reads a curated URL list, fetches and extracts clean text from each page (Trafilatura + Playwright fallback), chunks content into embedding-sized passages, generates vector embeddings via Ollama, and upserts everything into the `resource_chunks` table.

This follows the same ingestion pattern established by NPI ingestion: framework-agnostic Python module that takes a DB connection, does the work, returns a result. The FastAPI task endpoint (`/tasks/ingest-resources`) is added in Step 4 — this plan builds the core module only.

### Key decisions from evaluation

- **Tokenizer for chunking:** Word-count approximation (~1.3 tokens/word). No tiktoken dependency — the 300-600 token target is a soft guideline, not a hard cutoff.
- **Playwright:** Include in setup with `playwright install chromium`. Accept ~200MB disk for reliable JS fallback.
- **Upsert structure:** Refactor `api/ingestion/upsert.py` into a `upsert/` package with per-object-type modules.
- **Vector casting:** Use `register_vector(conn)` + plain `%s` in templates, consistent with existing `upsert_practices()`.

## Prerequisites

All verified present:
- Postgres + pgvector with `resource_chunks` table and HNSW index (migration `0001_initial.sql`)
- Ollama + `nomic-embed-text` on VPS, accessible via SSH tunnel
- `api/embeddings.py` with `generate_embedding()` and `get_embedding_model_version()`
- NPI ingestion pattern established in `api/ingestion/`

---

## Step 1: Refactor upsert module into a package

Before adding resource chunk upserts, restructure the upsert module so each object type has its own file.

### Changes

**Create `api/ingestion/upsert/` package:**

- `api/ingestion/upsert/__init__.py` — re-exports for backwards compatibility
- `api/ingestion/upsert/practices.py` — move existing code from `api/ingestion/upsert.py`
- Delete `api/ingestion/upsert.py` after migration

**`api/ingestion/upsert/__init__.py`:**
```python
from ingestion.upsert.practices import fetch_embedded_npi_numbers, upsert_practices

__all__ = ["fetch_embedded_npi_numbers", "upsert_practices"]
```

**`api/ingestion/upsert/practices.py`:**
Exact contents of current `api/ingestion/upsert.py` — no logic changes.

### Files

| File | Action | Description |
|------|--------|-------------|
| `api/ingestion/upsert/__init__.py` | Create | Re-exports for backwards compatibility |
| `api/ingestion/upsert/practices.py` | Create | Moved from `api/ingestion/upsert.py` |
| `api/ingestion/upsert.py` | Delete | Replaced by package |

### Verification

```bash
cd api && .venv/bin/pytest tests/test_ingest_npi.py -v
```

All existing tests must pass unchanged — imports like `from ingestion.upsert import upsert_practices` resolve through the package `__init__.py`.

---

## Step 2: Add dependencies (Trafilatura + Playwright)

### Changes

**`api/requirements.txt`** — add under a new comment block:
```
# Ingestion — resource extraction (Step 3)
trafilatura==2.*
playwright==1.*
```

**`Makefile`** — update `setup-api` target to install Playwright's Chromium after pip install:
```makefile
setup-api:
	cd api && python3 -m venv .venv
	cd api && .venv/bin/pip install -r requirements-dev.txt
	cd api && .venv/bin/playwright install chromium
```

### Files

| File | Action | Description |
|------|--------|-------------|
| `api/requirements.txt` | Modify | Add trafilatura, playwright |
| `Makefile` | Modify | Add `playwright install chromium` to `setup-api` |

### Verification

```bash
cd api && .venv/bin/pip install -r requirements.txt
cd api && .venv/bin/playwright install chromium
cd api && .venv/bin/python -c "import trafilatura; print('trafilatura OK')"
cd api && .venv/bin/python -c "from playwright.sync_api import sync_playwright; print('playwright OK')"
```

---

## Step 3: Content extraction module

Build the fetch-and-extract logic: Trafilatura primary, Playwright fallback, local markdown storage.

### Changes

**Create `api/ingestion/extract.py`:**

This module handles fetching and extracting clean text from URLs. It is used by `ingest_resources.py` but separated for testability.

```python
import hashlib
import logging
import os
import re

import trafilatura

logger = logging.getLogger(__name__)

RESOURCES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "resources")


def slugify_url(url: str) -> str:
    """Convert URL to a filesystem-safe slug for local storage."""
    slug = re.sub(r"https?://", "", url)
    slug = re.sub(r"[^a-zA-Z0-9]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:120]


def extract_content(url: str) -> dict | None:
    """Fetch and extract content from a URL. Returns dict with content and metadata, or None on failure.

    Tries Trafilatura first. Falls back to Playwright for JS-rendered pages.
    """
    result = _extract_with_trafilatura(url)
    if result:
        return result

    result = _extract_with_playwright(url)
    if result:
        return result

    logger.warning("Extraction failed for %s: both Trafilatura and Playwright returned empty", url)
    return None


def _extract_with_trafilatura(url: str) -> dict | None:
    """Attempt extraction via Trafilatura (HTTP fetch + boilerplate removal)."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return _parse_with_trafilatura(downloaded, url)


def _extract_with_playwright(url: str) -> dict | None:
    """Render page with Playwright, then extract with Trafilatura."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed, skipping fallback for %s", url)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            html = page.content()
            browser.close()
    except Exception as e:
        logger.warning("Playwright failed for %s: %s", url, e)
        return None

    return _parse_with_trafilatura(html, url)


def _parse_with_trafilatura(html: str, url: str) -> dict | None:
    """Parse HTML with Trafilatura and return extracted content with metadata."""
    content = trafilatura.extract(html, output_format="txt", include_links=False)
    if not content or len(content.strip()) < 100:
        return None

    metadata = trafilatura.extract(html, output_format="xml", include_links=False)
    title = ""
    if metadata:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(metadata)
            title = root.attrib.get("title", "")
        except ET.ParseError:
            pass

    return {"content": content, "title": title, "url": url}


def save_extracted_content(url: str, content: str) -> str:
    """Save extracted markdown to local file. Returns the file path."""
    os.makedirs(RESOURCES_DIR, exist_ok=True)
    slug = slugify_url(url)
    path = os.path.join(RESOURCES_DIR, f"{slug}.md")
    with open(path, "w") as f:
        f.write(content)
    return path
```

### Files

| File | Action | Description |
|------|--------|-------------|
| `api/ingestion/extract.py` | Create | URL fetching with Trafilatura + Playwright fallback |

### Tests

**Create `api/tests/test_extract.py`:**

```python
from ingestion.extract import slugify_url

def test_slugify_url_strips_protocol():
    assert slugify_url("https://example.com/page") == "example-com-page"

def test_slugify_url_truncates_long_urls():
    long_url = "https://example.com/" + "a" * 200
    assert len(slugify_url(long_url)) <= 120
```

Extraction functions that hit the network are tested via the integration test in Step 6. Unit tests here cover the pure functions only.

### Verification

```bash
cd api && .venv/bin/pytest tests/test_extract.py -v
```

---

## Step 4: Chunking module

Implement semantic chunking with contextual headers, splitting on section boundaries and targeting 300-600 tokens per chunk via word-count approximation.

### Changes

**Create `api/ingestion/chunking.py`:**

```python
import re

TOKENS_PER_WORD = 1.3
MIN_CHUNK_TOKENS = 300
MAX_CHUNK_TOKENS = 600
MIN_CHUNK_WORDS = int(MIN_CHUNK_TOKENS / TOKENS_PER_WORD)  # ~230
MAX_CHUNK_WORDS = int(MAX_CHUNK_TOKENS / TOKENS_PER_WORD)  # ~461

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def chunk_content(content: str, page_title: str = "") -> list[dict]:
    """Split content into chunks with contextual headers.

    Returns list of dicts with keys: content, chunk_index, section_header.
    Each chunk targets 300-600 tokens (estimated via word count).
    Splits on section headings first, then on paragraph boundaries within sections.
    """
    sections = _split_into_sections(content)
    chunks = []

    for section_header, section_text in sections:
        header = section_header or page_title
        section_chunks = _split_section_into_chunks(section_text, header)
        for chunk_text in section_chunks:
            chunks.append({
                "content": chunk_text,
                "chunk_index": len(chunks),
                "section_header": header,
            })

    return chunks


def _split_into_sections(content: str) -> list[tuple[str, str]]:
    """Split content by markdown headings. Returns list of (header, body) tuples."""
    parts = HEADING_PATTERN.split(content)

    if not HEADING_PATTERN.search(content):
        return [("", content.strip())]

    sections = []
    i = 0
    if parts[0].strip():
        sections.append(("", parts[0].strip()))
        i = 1
    else:
        i = 1

    while i < len(parts) - 2:
        header_text = parts[i + 1].strip()
        body = parts[i + 2].strip() if i + 2 < len(parts) else ""
        sections.append((header_text, body))
        i += 3

    return [(h, b) for h, b in sections if b]


def _split_section_into_chunks(text: str, header: str) -> list[str]:
    """Split a section into chunks targeting MIN-MAX word count.

    Splits on paragraph boundaries (double newline). Merges short paragraphs
    together; splits long paragraphs if they exceed the max.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks = []
    current_parts = []
    current_words = 0

    for para in paragraphs:
        para_words = len(para.split())

        if para_words > MAX_CHUNK_WORDS:
            if current_parts:
                chunks.append(_format_chunk(header, current_parts))
                current_parts = []
                current_words = 0
            for sub_chunk in _hard_split(para, MAX_CHUNK_WORDS):
                chunks.append(_format_chunk(header, [sub_chunk]))
            continue

        if current_words + para_words > MAX_CHUNK_WORDS and current_parts:
            chunks.append(_format_chunk(header, current_parts))
            current_parts = []
            current_words = 0

        current_parts.append(para)
        current_words += para_words

    if current_parts:
        if chunks and current_words < MIN_CHUNK_WORDS:
            last = chunks.pop()
            chunks.append(last + "\n\n" + "\n\n".join(current_parts))
        else:
            chunks.append(_format_chunk(header, current_parts))

    return chunks


def _format_chunk(header: str, parts: list[str]) -> str:
    """Prepend section header to chunk content."""
    body = "\n\n".join(parts)
    if header:
        return f"{header}\n\n{body}"
    return body


def _hard_split(text: str, max_words: int) -> list[str]:
    """Split text on sentence boundaries when it exceeds max_words."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current = []
    current_words = 0

    for sentence in sentences:
        s_words = len(sentence.split())
        if current_words + s_words > max_words and current:
            chunks.append(" ".join(current))
            current = []
            current_words = 0
        current.append(sentence)
        current_words += s_words

    if current:
        chunks.append(" ".join(current))

    return chunks
```

### Files

| File | Action | Description |
|------|--------|-------------|
| `api/ingestion/chunking.py` | Create | Semantic chunking with contextual headers |

### Tests

**Create `api/tests/test_chunking.py`:**

```python
from ingestion.chunking import chunk_content, _split_into_sections, MIN_CHUNK_WORDS, MAX_CHUNK_WORDS


def test_chunk_content_basic():
    content = "This is a paragraph. " * 100
    chunks = chunk_content(content, page_title="Test Page")
    assert len(chunks) >= 1
    for chunk in chunks:
        assert "content" in chunk
        assert "chunk_index" in chunk
        assert "section_header" in chunk


def test_chunk_content_preserves_section_headers():
    content = "# Section One\n\nParagraph one content. " * 50
    content += "\n\n# Section Two\n\nParagraph two content. " * 50
    chunks = chunk_content(content)
    headers = {c["section_header"] for c in chunks}
    assert "Section One" in headers
    assert "Section Two" in headers


def test_chunk_content_sequential_indexes():
    content = "Some content paragraph. " * 200
    chunks = chunk_content(content)
    for i, chunk in enumerate(chunks):
        assert chunk["chunk_index"] == i


def test_chunk_content_word_count_within_bounds():
    content = "A moderately long sentence with several words in it. " * 300
    chunks = chunk_content(content)
    for chunk in chunks:
        words = len(chunk["content"].split())
        # Allow some slack — header adds words, merging short tails is allowed
        assert words <= MAX_CHUNK_WORDS * 1.5, f"Chunk too large: {words} words"


def test_chunk_content_empty_input():
    assert chunk_content("") == []
    assert chunk_content("   ") == []


def test_split_into_sections_no_headings():
    sections = _split_into_sections("Just a plain paragraph.")
    assert len(sections) == 1
    assert sections[0][0] == ""


def test_split_into_sections_with_headings():
    content = "# First\n\nBody one.\n\n## Second\n\nBody two."
    sections = _split_into_sections(content)
    assert len(sections) == 2
    assert sections[0][0] == "First"
    assert sections[1][0] == "Second"


def test_page_title_used_when_no_section_header():
    content = "Paragraph without any headings. " * 50
    chunks = chunk_content(content, page_title="My Page Title")
    assert chunks[0]["section_header"] == "My Page Title"
```

### Verification

```bash
cd api && .venv/bin/pytest tests/test_chunking.py -v
```

---

## Step 5: Resource chunks upsert

Add the database upsert function for resource chunks, following the new package structure from Step 1.

### Changes

**Create `api/ingestion/upsert/resource_chunks.py`:**

```python
from psycopg2.extras import execute_values
from pgvector.psycopg2 import register_vector


def fetch_existing_content_hashes(conn) -> set[str]:
    """Return set of content_hash values already in resource_chunks."""
    cur = conn.cursor()
    cur.execute("SELECT content_hash FROM resource_chunks WHERE embedding IS NOT NULL")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result


def upsert_resource_chunks(conn, chunks: list[dict]) -> None:
    """Bulk upsert resource chunks with embeddings into resource_chunks table."""
    register_vector(conn)
    cur = conn.cursor()

    values = [
        (
            c["content_hash"], c["source_url"], c["org_name"], c["page_title"],
            c["county_scope"], c["fetch_date"], c["content"],
            c["chunk_index"], c["section_header"],
            c["embedding"], c["embedding_model"],
        )
        for c in chunks
    ]

    execute_values(
        cur,
        """
        INSERT INTO resource_chunks (
            content_hash, source_url, org_name, page_title,
            county_scope, fetch_date, content,
            chunk_index, section_header,
            embedding, embedding_model,
            updated_at
        ) VALUES %s
        ON CONFLICT (content_hash) DO UPDATE SET
            source_url = EXCLUDED.source_url,
            org_name = EXCLUDED.org_name,
            page_title = EXCLUDED.page_title,
            county_scope = EXCLUDED.county_scope,
            fetch_date = EXCLUDED.fetch_date,
            chunk_index = EXCLUDED.chunk_index,
            section_header = EXCLUDED.section_header,
            embedding = EXCLUDED.embedding,
            embedding_model = EXCLUDED.embedding_model,
            updated_at = NOW()
        """,
        values,
        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        page_size=100,
    )

    conn.commit()
    cur.close()
```

**Update `api/ingestion/upsert/__init__.py`:**
```python
from ingestion.upsert.practices import fetch_embedded_npi_numbers, upsert_practices
from ingestion.upsert.resource_chunks import fetch_existing_content_hashes, upsert_resource_chunks

__all__ = [
    "fetch_embedded_npi_numbers",
    "upsert_practices",
    "fetch_existing_content_hashes",
    "upsert_resource_chunks",
]
```

### Files

| File | Action | Description |
|------|--------|-------------|
| `api/ingestion/upsert/resource_chunks.py` | Create | Bulk upsert for resource chunks |
| `api/ingestion/upsert/__init__.py` | Modify | Add resource chunk exports |

### Tests

**Create `api/tests/test_upsert_resource_chunks.py`:**

```python
from datetime import date

from ingestion.upsert.resource_chunks import (
    fetch_existing_content_hashes,
    upsert_resource_chunks,
)


def make_chunk(**overrides) -> dict:
    chunk = {
        "content_hash": "abc123def456",
        "source_url": "https://example.com/page",
        "org_name": "Test Org",
        "page_title": "Test Page",
        "county_scope": "national",
        "fetch_date": date.today(),
        "content": "This is test chunk content.",
        "chunk_index": 0,
        "section_header": "Test Section",
        "embedding": [0.1] * 768,
        "embedding_model": "test-model",
    }
    chunk.update(overrides)
    return chunk


def test_upsert_and_fetch_hashes(db_conn):
    chunk = make_chunk()
    upsert_resource_chunks(db_conn, [chunk])

    hashes = fetch_existing_content_hashes(db_conn)
    assert "abc123def456" in hashes


def test_upsert_is_idempotent(db_conn):
    chunk = make_chunk()
    upsert_resource_chunks(db_conn, [chunk])
    upsert_resource_chunks(db_conn, [chunk])

    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM resource_chunks WHERE content_hash = 'abc123def456'")
    assert cur.fetchone()[0] == 1


def test_upsert_updates_embedding_on_conflict(db_conn):
    chunk = make_chunk()
    upsert_resource_chunks(db_conn, [chunk])

    updated = {**chunk, "embedding_model": "new-model"}
    upsert_resource_chunks(db_conn, [updated])

    cur = db_conn.cursor()
    cur.execute("SELECT embedding_model FROM resource_chunks WHERE content_hash = 'abc123def456'")
    assert cur.fetchone()[0] == "new-model"


def test_fetch_hashes_excludes_null_embeddings(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO resource_chunks (content_hash, source_url, content) "
        "VALUES ('no-embedding-hash', 'https://example.com', 'content') "
        "ON CONFLICT (content_hash) DO NOTHING"
    )
    hashes = fetch_existing_content_hashes(db_conn)
    assert "no-embedding-hash" not in hashes
```

### Verification

```bash
cd api && .venv/bin/pytest tests/test_upsert_resource_chunks.py tests/test_ingest_npi.py -v
```

Both the new resource chunk tests and existing NPI tests must pass.

---

## Step 6: Main ingestion module + Makefile target

Wire everything together in `api/ingestion/ingest_resources.py` and add a Makefile target.

### Changes

**Create `api/ingestion/ingest_resources.py`:**

```python
import argparse
import hashlib
import json
import logging
from datetime import date

from db.connection import get_connection
from embeddings import generate_embedding, get_embedding_model_version
from ingestion.chunking import chunk_content
from ingestion.extract import extract_content, save_extracted_content
from ingestion.upsert.resource_chunks import upsert_resource_chunks

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

BATCH_SIZE = 50


def load_url_list(path: str) -> list[dict]:
    """Load curated URL list from JSON file."""
    with open(path) as f:
        return json.load(f)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def ingest_resources(url_list_path: str) -> dict:
    """Main ingestion function. Returns a summary dict."""
    urls = load_url_list(url_list_path)
    conn = get_connection()
    model_version = get_embedding_model_version()
    today = date.today()

    results = {
        "total_urls": len(urls),
        "successful_extractions": 0,
        "failed_urls": [],
        "total_chunks": 0,
        "total_upserted": 0,
    }

    all_chunks = []

    for entry in urls:
        url = entry["url"]
        org_name = entry.get("org_name", "")
        county_scope = entry.get("county_scope", "national")

        logger.info("Extracting: %s", url)
        extracted = extract_content(url)
        if not extracted:
            results["failed_urls"].append(url)
            continue

        results["successful_extractions"] += 1
        save_extracted_content(url, extracted["content"])

        page_title = extracted.get("title", "") or entry.get("page_title", "")
        chunks = chunk_content(extracted["content"], page_title=page_title)

        for chunk in chunks:
            chunk["content_hash"] = content_hash(chunk["content"])
            chunk["source_url"] = url
            chunk["org_name"] = org_name
            chunk["page_title"] = page_title
            chunk["county_scope"] = county_scope
            chunk["fetch_date"] = today
            all_chunks.append(chunk)

    results["total_chunks"] = len(all_chunks)

    # Embed and upsert in batches
    for i in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[i:i + BATCH_SIZE]
        for chunk in batch:
            chunk["embedding"] = generate_embedding(chunk["content"])
            chunk["embedding_model"] = model_version
        upsert_resource_chunks(conn, batch)
        done = min(i + BATCH_SIZE, len(all_chunks))
        logger.info("Embedded and upserted %d/%d chunks.", done, len(all_chunks))

    results["total_upserted"] = len(all_chunks)
    conn.close()

    logger.info(
        "Ingestion complete: %d/%d URLs extracted, %d chunks upserted, %d failures",
        results["successful_extractions"],
        results["total_urls"],
        results["total_upserted"],
        len(results["failed_urls"]),
    )
    if results["failed_urls"]:
        logger.warning("Failed URLs: %s", results["failed_urls"])

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url-list", required=True, help="Path to curated URL list JSON")
    args = parser.parse_args()
    ingest_resources(args.url_list)
```

**Create `data/resource_urls.json`** — seed file with an initial small batch for testing (the full 50-100 URL list is curated separately):

```json
[
  {
    "url": "https://autisticadvocacy.org/about-asan/about-autism/",
    "org_name": "ASAN",
    "county_scope": "national"
  },
  {
    "url": "https://autisticadvocacy.org/about-asan/identity-first-language/",
    "org_name": "ASAN",
    "county_scope": "national"
  },
  {
    "url": "https://www.understood.org/en/articles/what-is-an-iep",
    "org_name": "Understood",
    "county_scope": "national"
  }
]
```

This seed file is a starting point — the full curated list will be expanded to 50-100 URLs as a separate content curation task.

**`Makefile`** — add `ingest-resources` target:

```makefile
ingest-resources:
	@echo "Requires SSH tunnel (make tunnel)."
	cd api && .venv/bin/python -m ingestion.ingest_resources --url-list ../data/resource_urls.json
```

Also add to `.PHONY` line.

### Files

| File | Action | Description |
|------|--------|-------------|
| `api/ingestion/ingest_resources.py` | Create | Main ingestion orchestrator |
| `data/resource_urls.json` | Create | Seed URL list (start small, expand later) |
| `Makefile` | Modify | Add `ingest-resources` target |

### Tests

**Create `api/tests/test_ingest_resources.py`:**

```python
import json
import os
import tempfile
from unittest.mock import patch

from ingestion.ingest_resources import content_hash, load_url_list, ingest_resources


def test_content_hash_deterministic():
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("world")


def test_content_hash_is_sha256():
    import hashlib
    expected = hashlib.sha256(b"test").hexdigest()
    assert content_hash("test") == expected


def test_load_url_list():
    data = [{"url": "https://example.com", "org_name": "Test"}]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    try:
        result = load_url_list(path)
        assert result == data
    finally:
        os.unlink(path)


def test_ingest_resources_handles_extraction_failures(db_conn):
    """When extraction fails for all URLs, ingestion completes with zero chunks."""
    data = [{"url": "https://nonexistent.invalid/page", "org_name": "Test", "county_scope": "national"}]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name

    try:
        with patch("ingestion.ingest_resources.get_connection", return_value=db_conn), \
             patch("ingestion.ingest_resources.extract_content", return_value=None):
            result = ingest_resources(path)

        assert result["total_urls"] == 1
        assert result["successful_extractions"] == 0
        assert len(result["failed_urls"]) == 1
        assert result["total_chunks"] == 0
    finally:
        os.unlink(path)


def test_ingest_resources_end_to_end(db_conn):
    """Full pipeline with mocked extraction and embedding."""
    data = [{"url": "https://example.com/article", "org_name": "Test Org", "county_scope": "national"}]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name

    fake_content = "# Test Article\n\n" + "This is test content for chunking. " * 100
    fake_embedding = [0.1] * 768

    try:
        with patch("ingestion.ingest_resources.get_connection", return_value=db_conn), \
             patch("ingestion.ingest_resources.extract_content", return_value={"content": fake_content, "title": "Test Article", "url": "https://example.com/article"}), \
             patch("ingestion.ingest_resources.save_extracted_content"), \
             patch("ingestion.ingest_resources.generate_embedding", return_value=fake_embedding), \
             patch("ingestion.ingest_resources.get_embedding_model_version", return_value="test-v1"):
            result = ingest_resources(path)

        assert result["successful_extractions"] == 1
        assert result["total_chunks"] > 0
        assert result["total_upserted"] == result["total_chunks"]
        assert len(result["failed_urls"]) == 0

        # Verify chunks are in the database
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM resource_chunks WHERE source_url = 'https://example.com/article'")
        assert cur.fetchone()[0] == result["total_chunks"]
    finally:
        os.unlink(path)
```

### Verification

```bash
# Unit and integration tests
cd api && .venv/bin/pytest tests/test_ingest_resources.py -v

# All tests still pass
cd api && .venv/bin/pytest tests/ -v

# Live test against VPS (requires tunnel + Ollama)
make ingest-resources
```

The live `make ingest-resources` run against the seed URLs is the final acceptance test — it exercises extraction, chunking, embedding, and upsert end-to-end with real data.

---

## Step 7: Spot-check verification script

Add a post-ingestion verification script (following the pattern of `verify/npi_ingestion.py`) to confirm chunks are well-formed and retrievable.

### Changes

**Create `api/verify/resource_ingestion.py`:**

A pytest-based verification script that checks:
- `resource_chunks` table has rows
- All rows have non-null embeddings
- Embeddings are 768-dimensional
- A sample similarity query returns results
- Chunk content is non-empty and within expected size bounds

**`Makefile`** — add `verify-resources` target and update `verify`:

```makefile
verify-resources:
	@echo "Requires SSH tunnel (make tunnel)."
	cd api && .venv/bin/pytest verify/resource_ingestion.py -v

verify: verify-infra verify-npi verify-resources
```

### Files

| File | Action | Description |
|------|--------|-------------|
| `api/verify/resource_ingestion.py` | Create | Post-ingestion data validation |
| `Makefile` | Modify | Add `verify-resources` target |

### Verification

```bash
# After running make ingest-resources:
make verify-resources
```

---

## Files Summary

| File | Action | Description |
|------|--------|-------------|
| `api/ingestion/upsert/__init__.py` | Create | Package init with re-exports |
| `api/ingestion/upsert/practices.py` | Create | Moved from `upsert.py` |
| `api/ingestion/upsert.py` | Delete | Replaced by package |
| `api/requirements.txt` | Modify | Add trafilatura, playwright |
| `Makefile` | Modify | Add setup, ingest, verify targets |
| `api/ingestion/extract.py` | Create | URL fetching + extraction |
| `api/ingestion/chunking.py` | Create | Semantic chunking with headers |
| `api/ingestion/upsert/resource_chunks.py` | Create | Resource chunks bulk upsert |
| `api/ingestion/ingest_resources.py` | Create | Main ingestion orchestrator |
| `data/resource_urls.json` | Create | Seed URL list |
| `api/verify/resource_ingestion.py` | Create | Post-ingestion verification |
| `api/tests/test_extract.py` | Create | Extraction unit tests |
| `api/tests/test_chunking.py` | Create | Chunking unit tests |
| `api/tests/test_upsert_resource_chunks.py` | Create | Upsert integration tests |
| `api/tests/test_ingest_resources.py` | Create | End-to-end ingestion tests |

---

## Completion checklist

- [ ] Step 1: Refactor upsert module into a package
- [ ] Step 2: Add dependencies (Trafilatura + Playwright)
- [ ] Step 3: Content extraction module
- [ ] Step 4: Chunking module
- [ ] Step 5: Resource chunks upsert
- [ ] Step 6: Main ingestion module + Makefile target
- [ ] Step 7: Spot-check verification script
