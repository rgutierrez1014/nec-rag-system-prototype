import csv
import io
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from ingestion.ingest_npi import (
    _title_address,
    compose_description,
    filter_and_transform,
    transform_npi_row,
)
from ingestion.embedding import build_embedding_input, embed_practices
from ingestion.upsert import fetch_embedded_npi_numbers, upsert_practices


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_npi_row(**overrides) -> dict:
    """Minimal valid NPI CSV row for a Type 1 (individual) provider."""
    row = {
        "NPI": "1234567890",
        "Entity Type Code": "1",
        "Provider Organization Name (Legal Business Name)": "",
        "Provider First Name": "Jane",
        "Provider Last Name (Legal Name)": "Smith",
        "Provider Credential Text": "LCSW",
        "Provider First Line Business Practice Location Address": "123 Main St",
        "Provider Business Practice Location Address City Name": "SAN FRANCISCO",
        "Provider Business Practice Location Address State Name": "CA",
        "Provider Business Practice Location Address Postal Code": "94110-1234",
        "NPI Deactivation Date": "",
        "Healthcare Provider Taxonomy Code_1": "101Y00000X",
        **{f"Healthcare Provider Taxonomy Code_{i}": "" for i in range(2, 16)},
    }
    row.update(overrides)
    return row


def make_practice(**overrides) -> dict:
    """Minimal enriched practice dict ready for embedding/upsert."""
    practice = {
        "npi_number": "1234567890",
        "name": "Jane Smith, LCSW",
        "description": "Jane Smith, LCSW is a mental health counseling practice in San Francisco.",
        "practice_type": "healthcare",
        "address_1": "123 Main St",
        "address_city": "San Francisco",
        "address_state": "CA",
        "address_zip": "94110",
        "neighborhood": "Mission",
        "services": ["mental-health-counseling"],
        "specialties": [],
        "presence_types": ["in-person"],
        "professionals": [{"name": "Jane Smith", "credentials": "LCSW", "npi_number": "1234567890"}],
    }
    practice.update(overrides)
    return practice


# ── _title_address ─────────────────────────────────────────────────────────────

def test_title_address_basic():
    assert _title_address("123 MAIN ST") == "123 Main St"


def test_title_address_fixes_ordinal_suffixes():
    assert _title_address("24TH ST") == "24th St"
    assert _title_address("1ST AVE") == "1st Ave"
    assert _title_address("3RD ST") == "3rd St"
    assert _title_address("2ND FLOOR") == "2nd Floor"


# ── transform_npi_row ──────────────────────────────────────────────────────────

def test_transform_individual_provider():
    row = make_npi_row()
    practice = transform_npi_row(row)

    assert practice["npi_number"] == "1234567890"
    assert practice["name"] == "Jane Smith, LCSW"
    assert practice["address_zip"] == "94110"
    assert practice["services"] == ["mental-health-counseling"]
    assert practice["specialties"] == []
    assert practice["presence_types"] == ["in-person"]
    assert len(practice["professionals"]) == 1
    assert practice["professionals"][0]["name"] == "Jane Smith"
    assert practice["professionals"][0]["credentials"] == "LCSW"
    assert practice["professionals"][0]["npi_number"] == "1234567890"


def test_transform_individual_no_credential():
    row = make_npi_row(**{"Provider Credential Text": ""})
    practice = transform_npi_row(row)
    assert practice["name"] == "Jane Smith"


def test_transform_organization():
    row = make_npi_row(**{
        "Entity Type Code": "2",
        "Provider Organization Name (Legal Business Name)": "ACME THERAPY GROUP",
    })
    practice = transform_npi_row(row)
    assert practice["name"] == "Acme Therapy Group"
    assert practice["professionals"] == []


def test_transform_maps_multiple_taxonomies():
    row = make_npi_row(**{
        "Healthcare Provider Taxonomy Code_1": "101Y00000X",
        "Healthcare Provider Taxonomy Code_2": "2084P0800X",
    })
    practice = transform_npi_row(row)
    assert set(practice["services"]) == {"mental-health-counseling", "psychiatry"}


def test_transform_zip_truncated_to_5_digits():
    row = make_npi_row(**{"Provider Business Practice Location Address Postal Code": "94110-9999"})
    practice = transform_npi_row(row)
    assert practice["address_zip"] == "94110"


# ── filter_and_transform ───────────────────────────────────────────────────────

NPI_CSV_COLUMNS = [
    "NPI", "Entity Type Code",
    "Provider Organization Name (Legal Business Name)",
    "Provider First Name", "Provider Last Name (Legal Name)", "Provider Credential Text",
    "Provider First Line Business Practice Location Address",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "NPI Deactivation Date",
    *[f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 16)],
]


def write_npi_csv(rows: list[dict]) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.DictWriter(f, fieldnames=NPI_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    f.close()
    return f.name


def test_filter_excludes_deactivated():
    path = write_npi_csv([make_npi_row(**{"NPI Deactivation Date": "20230101"})])
    try:
        assert filter_and_transform(path) == []
    finally:
        os.unlink(path)


def test_filter_excludes_wrong_city():
    path = write_npi_csv([make_npi_row(**{
        "Provider Business Practice Location Address City Name": "LOS ANGELES",
    })])
    try:
        assert filter_and_transform(path) == []
    finally:
        os.unlink(path)


def test_filter_excludes_no_matching_taxonomy():
    path = write_npi_csv([make_npi_row(**{
        "Healthcare Provider Taxonomy Code_1": "207Q00000X",  # family medicine — not in map
        **{f"Healthcare Provider Taxonomy Code_{i}": "" for i in range(2, 16)},
    })])
    try:
        assert filter_and_transform(path) == []
    finally:
        os.unlink(path)


def test_filter_includes_matching_sf_provider():
    path = write_npi_csv([make_npi_row()])
    try:
        result = filter_and_transform(path)
        assert len(result) == 1
        assert result[0]["npi_number"] == "1234567890"
    finally:
        os.unlink(path)


# ── compose_description ────────────────────────────────────────────────────────

def test_compose_description_with_neighborhood():
    practice = make_practice(neighborhood="Mission")
    desc = compose_description(practice)
    assert "Mission" in desc
    assert "San Francisco" in desc
    assert "Jane Smith, LCSW" in desc


def test_compose_description_without_neighborhood():
    practice = make_practice(neighborhood="")
    desc = compose_description(practice)
    assert "neighborhood" not in desc
    assert "San Francisco, CA" in desc


# ── build_embedding_input ──────────────────────────────────────────────────────

def test_build_embedding_input_includes_description_and_services():
    practice = make_practice()
    text = build_embedding_input(practice)
    assert practice["description"] in text
    assert "mental-health-counseling" in text


def test_build_embedding_input_includes_professionals():
    practice = make_practice()
    text = build_embedding_input(practice)
    assert "Jane Smith" in text


def test_build_embedding_input_no_professionals():
    practice = make_practice(professionals=[])
    text = build_embedding_input(practice)
    assert "Professionals" not in text


# ── embed_practices ────────────────────────────────────────────────────────────

def test_embed_practices_attaches_embedding_and_model():
    practice = make_practice()
    fake_embedding = [0.1] * 768

    with patch("ingestion.embedding.generate_embedding", return_value=fake_embedding), \
         patch("ingestion.embedding.get_embedding_model_version", return_value="abc123"), \
         patch("ingestion.embedding.upsert_practices") as mock_upsert:
        embed_practices([practice], conn=None)

    assert practice["embedding"] == fake_embedding
    assert practice["embedding_model"] == "abc123"
    mock_upsert.assert_called_once()


# ── upsert_practices / fetch_embedded_npi_numbers ─────────────────────────────

def test_upsert_and_fetch_embedded(db_conn):
    practice = make_practice(embedding=[0.1] * 768, embedding_model="test-model")
    upsert_practices(db_conn, [practice])

    embedded = fetch_embedded_npi_numbers(db_conn)
    assert "1234567890" in embedded


def test_upsert_is_idempotent(db_conn):
    practice = make_practice(embedding=[0.1] * 768, embedding_model="test-model")
    upsert_practices(db_conn, [practice])
    upsert_practices(db_conn, [practice])

    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM practices WHERE npi_number = '1234567890'")
    assert cur.fetchone()[0] == 1


def test_upsert_updates_existing_record(db_conn):
    practice = make_practice(embedding=[0.1] * 768, embedding_model="test-model")
    upsert_practices(db_conn, [practice])

    updated = {**practice, "name": "Jane Smith Updated", "embedding_model": "new-model"}
    upsert_practices(db_conn, [updated])

    cur = db_conn.cursor()
    cur.execute("SELECT name, embedding_model FROM practices WHERE npi_number = '1234567890'")
    row = cur.fetchone()
    assert row[0] == "Jane Smith Updated"
    assert row[1] == "new-model"


def test_fetch_embedded_excludes_null_embeddings(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO practices (npi_number, name) VALUES ('9999999991', 'No Embedding') "
        "ON CONFLICT (npi_number) DO NOTHING"
    )
    embedded = fetch_embedded_npi_numbers(db_conn)
    assert "9999999991" not in embedded
