from psycopg2.extras import execute_values
from pgvector.psycopg2 import register_vector


def fetch_existing_content_hashes(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT content_hash FROM resource_chunks WHERE embedding IS NOT NULL")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result


def upsert_resource_chunks(conn, chunks: list[dict]) -> None:
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