"""
End-to-end verification: generate an embedding via Ollama, write it to
pgvector, query it back via similarity search.

Usage:
    make verify

Requires POSTGRES_* and OLLAMA_BASE_URL env vars (see .env.example).
"""

import os
import sys

import httpx
import psycopg2
from pgvector.psycopg2 import register_vector

from db.connection import get_connection


OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")


def generate_embedding(text: str, prefix: str = "search_document") -> list[float]:
    response = httpx.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": f"{prefix}: {text}"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["embedding"]


def main():
    print("1. Connecting to Postgres...")
    conn = get_connection()
    register_vector(conn)
    cur = conn.cursor()

    print("2. Verifying pgvector extension...")
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    if not row:
        print("FAIL: pgvector extension not installed")
        sys.exit(1)
    print(f"   pgvector version: {row[0]}")

    print("3. Generating test embedding via Ollama...")
    test_text = "Occupational therapy practice in San Francisco specializing in sensory integration"
    embedding = generate_embedding(test_text)
    dim = len(embedding)
    print(f"   Embedding dimension: {dim}")
    if dim != 768:
        print(f"FAIL: Expected 768 dimensions, got {dim}")
        sys.exit(1)

    print("4. Inserting test practice record...")
    cur.execute(
        """
        INSERT INTO practices (npi_number, name, description, address_city, address_state,
                               address_zip, services, embedding, embedding_model)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (npi_number) DO UPDATE SET embedding = EXCLUDED.embedding
        RETURNING id
        """,
        (
            "0000000001",
            "Test OT Practice",
            test_text,
            "San Francisco",
            "CA",
            "94110",
            ["occupational-therapy"],
            embedding,
            EMBEDDING_MODEL,
        ),
    )
    record_id = cur.fetchone()[0]
    conn.commit()
    print(f"   Inserted practice id={record_id}")

    print("5. Querying via similarity search...")
    query_text = "OT for kids with sensory issues"
    query_embedding = generate_embedding(query_text, prefix="search_query")
    cur.execute(
        """
        SELECT id, name, 1 - (embedding <=> %s) AS similarity
        FROM practices
        ORDER BY embedding <=> %s
        LIMIT 5
        """,
        (query_embedding, query_embedding),
    )
    results = cur.fetchall()
    print(f"   Found {len(results)} results:")
    for row in results:
        print(f"     id={row[0]} name={row[1]!r} similarity={row[2]:.4f}")

    print("6. Cleaning up test data...")
    cur.execute("DELETE FROM practices WHERE npi_number = '0000000001'")
    conn.commit()

    cur.close()
    conn.close()

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
