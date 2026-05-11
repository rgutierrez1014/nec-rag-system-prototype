"""
Integration tests for the database schema and vector operations.
Requires SSH tunnel to VPS (make tunnel).
"""

import pytest
from pgvector.psycopg2 import register_vector


def test_pgvector_extension_installed(db_conn):
    cur = db_conn.cursor()
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    assert row is not None, "pgvector extension not installed"


def test_practices_table_exists(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'practices')"
    )
    assert cur.fetchone()[0] is True


def test_resource_chunks_table_exists(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'resource_chunks')"
    )
    assert cur.fetchone()[0] is True


def test_practices_embedding_column_is_768(db_conn):
    cur = db_conn.cursor()
    cur.execute("""
        SELECT atttypmod FROM pg_attribute
        JOIN pg_class ON pg_attribute.attrelid = pg_class.oid
        WHERE pg_class.relname = 'practices' AND pg_attribute.attname = 'embedding'
    """)
    row = cur.fetchone()
    assert row is not None
    assert row[0] == 768, f"Expected vector(768), got vector({row[0]})"


def test_insert_and_query_vector(db_conn):
    cur = db_conn.cursor()
    fake_embedding = [0.1] * 768

    cur.execute(
        """
        INSERT INTO practices (npi_number, name, embedding, embedding_model)
        VALUES ('9999999999', 'Test Practice', %s, 'test-model')
        ON CONFLICT (npi_number) DO UPDATE SET embedding = EXCLUDED.embedding
        RETURNING id
        """,
        (fake_embedding,),
    )
    record_id = cur.fetchone()[0]
    assert record_id is not None

    cur.execute(
        """
        SELECT id, name, 1 - (embedding <=> %s::vector) AS similarity
        FROM practices
        WHERE id = %s
        """,
        (fake_embedding, record_id),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[2] == pytest.approx(1.0, abs=0.001)
