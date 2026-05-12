"""
Post-ingestion verification for the NPI ingestion pipeline.
Runs against nec_rag_dev — requires SSH tunnel and a completed or partial ingestion run.
Execute via: make verify-npi
"""

import os

import psycopg2
import pytest
from pgvector.psycopg2 import register_vector


VALID_SERVICE_SLUGS = {
    "psychology",
    "mental-health-counseling",
    "marriage-family-therapy",
    "psychiatry",
    "occupational-therapy",
    "speech-language-pathology",
    "psychiatric-nursing",
    "social-work",
    "developmental-behavioral-pediatrics",
    "physical-therapy",
    "audiology",
}


@pytest.fixture(scope="module")
def dev_conn():
    conn = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
    register_vector(conn)
    yield conn
    conn.close()


def test_practices_exist(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices")
    count = cur.fetchone()[0]
    assert count > 0, "No practices found — has ingestion been run?"


def test_all_practices_have_embeddings(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices WHERE embedding IS NULL")
    missing = cur.fetchone()[0]
    assert missing == 0, f"{missing} practices have no embedding"


def test_all_practices_have_embedding_model(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices WHERE embedding_model IS NULL OR embedding_model = ''")
    missing = cur.fetchone()[0]
    assert missing == 0, f"{missing} practices have no embedding_model"


def test_embedding_model_is_consistent(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT DISTINCT embedding_model FROM practices")
    models = [row[0] for row in cur.fetchall()]
    assert len(models) == 1, f"Expected one embedding model, found: {models}"


def test_embeddings_are_768_dimensional(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT embedding FROM practices LIMIT 10")
    for (embedding,) in cur.fetchall():
        assert len(embedding) == 768, f"Expected 768 dimensions, got {len(embedding)}"


def test_services_contain_only_valid_slugs(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT DISTINCT unnest(services) FROM practices")
    slugs = {row[0] for row in cur.fetchall()}
    invalid = slugs - VALID_SERVICE_SLUGS
    assert not invalid, f"Invalid service slugs found: {invalid}"


def test_specialties_are_empty(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices WHERE array_length(specialties, 1) > 0")
    non_empty = cur.fetchone()[0]
    assert non_empty == 0, f"{non_empty} practices have non-empty specialties (should be [] for NPI data)"


def test_neighborhood_coverage(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM practices WHERE neighborhood != ''")
    with_neighborhood = cur.fetchone()[0]
    coverage = with_neighborhood / total if total else 0
    assert coverage >= 0.8, f"Neighborhood coverage {coverage:.0%} is below 80% ({with_neighborhood}/{total})"


def test_all_practices_in_san_francisco(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices WHERE address_city != 'San Francisco' OR address_state != 'CA'")
    wrong_location = cur.fetchone()[0]
    assert wrong_location == 0, f"{wrong_location} practices have unexpected city/state"


def test_npi_numbers_are_unique(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT npi_number) FROM practices")
    unique = cur.fetchone()[0]
    assert total == unique, f"Duplicate NPI numbers found ({total} rows, {unique} unique)"


def test_no_empty_names(dev_conn):
    cur = dev_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices WHERE name = '' OR name IS NULL")
    empty = cur.fetchone()[0]
    assert empty == 0, f"{empty} practices have empty names"
