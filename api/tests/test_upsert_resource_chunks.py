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