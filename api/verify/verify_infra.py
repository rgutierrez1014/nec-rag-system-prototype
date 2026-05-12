"""
End-to-end infrastructure verification: Postgres, pgvector, and Ollama embeddings.
Runs against nec_rag_dev — requires SSH tunnel.
Execute via: make verify-infra
"""

import os

import psycopg2
import pytest
from pgvector.psycopg2 import register_vector

from db.connection import get_connection
from embeddings import generate_embedding, EMBEDDING_MODEL


@pytest.fixture(scope="module")
def dev_conn():
    conn = get_connection()
    register_vector(conn)
    yield conn
    conn.close()


def test_postgres_connection(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1


def test_pgvector_extension_installed(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    assert row is not None, "pgvector extension not installed"


def test_ollama_returns_768_dimensional_embedding():
    embedding = generate_embedding("Occupational therapy practice in San Francisco")
    assert len(embedding) == 768


def test_embedding_roundtrip(dev_conn):
    embedding = generate_embedding("Occupational therapy practice in San Francisco")
    cur = dev_conn.cursor()
    cur.execute(
        """
        INSERT INTO practices (npi_number, name, description, address_city, address_state,
                               address_zip, services, embedding, embedding_model)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (npi_number) DO UPDATE SET embedding = EXCLUDED.embedding
        RETURNING id
        """,
        (
            "0000000001", "Test OT Practice",
            "Occupational therapy practice in San Francisco",
            "San Francisco", "CA", "94110",
            ["occupational-therapy"], embedding, EMBEDDING_MODEL,
        ),
    )
    record_id = cur.fetchone()[0]
    dev_conn.commit()
    assert record_id is not None

    query_embedding = generate_embedding("OT for kids with sensory issues", prefix="search_query")
    cur.execute(
        """
        SELECT id, 1 - (embedding <=> %s::vector) AS similarity
        FROM practices
        WHERE npi_number = '0000000001'
        """,
        (query_embedding,),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[1] > 0.3, f"Similarity too low: {row[1]:.4f}"

    cur.execute("DELETE FROM practices WHERE npi_number = '0000000001'")
    dev_conn.commit()
