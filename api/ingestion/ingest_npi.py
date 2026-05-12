import argparse
import csv
import json
import os
import re

from db.connection import get_connection
from ingestion.embedding import embed_practices
from ingestion.geocoding import geocode_practices
from ingestion.neighborhoods import enrich_with_neighborhoods, load_neighborhoods
from ingestion.upsert import fetch_embedded_npi_numbers


TAXONOMY_TO_SERVICE = {
    "103T": "psychology",
    "101Y": "mental-health-counseling",
    "106H": "marriage-family-therapy",
    "2084P": "psychiatry",
    "225X": "occupational-therapy",
    "235Z": "speech-language-pathology",
    "364S": "psychiatric-nursing",
    "1041": "social-work",
    "2080P0006": "developmental-behavioral-pediatrics",
    "2251": "physical-therapy",
    "231H": "audiology",
    # ABA (Applied Behavior Analysis) intentionally excluded — see SPEC.md guardrails
}

TAXONOMY_PREFIXES = tuple(TAXONOMY_TO_SERVICE.keys())

# Fixes title-case mangling of ordinal suffixes (24Th St → 24th St)
_ORDINAL_RE = re.compile(r'(\d+)(St|Nd|Rd|Th)\b')


def _title_address(address: str) -> str:
    return _ORDINAL_RE.sub(lambda m: m.group(1) + m.group(2).lower(), address.title())


def _row_taxonomy_codes(row: dict) -> list[str]:
    codes = [row.get(f"Healthcare Provider Taxonomy Code_{i}", "") for i in range(1, 16)]
    return [c for c in codes if c]


def _row_matches_taxonomy(codes: list[str]) -> bool:
    return any(code.startswith(TAXONOMY_PREFIXES) for code in codes)


def transform_npi_row(row: dict) -> dict:
    taxonomy_codes = _row_taxonomy_codes(row)
    services = list({
        slug
        for code in taxonomy_codes
        for prefix, slug in TAXONOMY_TO_SERVICE.items()
        if code.startswith(prefix)
    })

    entity_type = row["Entity Type Code"]
    if entity_type == "2":
        name = row["Provider Organization Name (Legal Business Name)"].strip().title()
        professionals = []
    else:
        first = row["Provider First Name"].strip().title()
        last = row["Provider Last Name (Legal Name)"].strip().title()
        credential = row.get("Provider Credential Text", "").strip()
        name = f"{first} {last}, {credential}" if credential else f"{first} {last}"
        professionals = [{"name": f"{first} {last}", "credentials": credential, "npi_number": row["NPI"].strip()}]

    zip_code = row["Provider Business Practice Location Address Postal Code"].strip()[:5]

    return {
        "npi_number": row["NPI"].strip(),
        "name": name,
        "description": "",
        "practice_type": "healthcare",
        "address_1": _title_address(row["Provider First Line Business Practice Location Address"].strip()),
        "address_city": "San Francisco",
        "address_state": "CA",
        "address_zip": zip_code,
        "neighborhood": "",
        "services": services,
        "specialties": [],
        "presence_types": ["in-person"],
        "professionals": professionals,
    }


def filter_and_transform(csv_path: str) -> list[dict]:
    practices = []
    i = 1
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if i % 10000 == 0:
                print(f"  Parsed {i} rows.")
            i += 1
            if row.get("NPI Deactivation Date", "").strip():
                continue
            city = row.get("Provider Business Practice Location Address City Name", "").strip().upper()
            state = row.get("Provider Business Practice Location Address State Name", "").strip().upper()
            if city != "SAN FRANCISCO" or state != "CA":
                continue
            codes = _row_taxonomy_codes(row)
            if not _row_matches_taxonomy(codes):
                continue
            practices.append(transform_npi_row(row))
    print(f"Parsed {i} rows total.")
    return practices


def write_jsonl(practices: list[dict], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True) if os.path.dirname(output_path) else None
    with open(output_path, "w", encoding="utf-8") as f:
        for practice in practices:
            f.write(json.dumps(practice) + "\n")


def load_jsonl(jsonl_path: str) -> list[dict]:
    with open(jsonl_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def compose_description(practice: dict) -> str:
    name = practice["name"]
    services_text = " and ".join(slug.replace("-", " ") for slug in practice["services"])
    neighborhood = practice["neighborhood"]

    if neighborhood:
        return (
            f"{name} is a {services_text} practice located in the "
            f"{neighborhood} neighborhood of San Francisco "
            f"({practice['address_1']}, {practice['address_zip']})."
        )
    return (
        f"{name} is a {services_text} practice located in "
        f"San Francisco, CA ({practice['address_1']}, {practice['address_zip']})."
    )


JSONL_PATH = "../data/npi_practices.jsonl"
GEOCODE_CACHE_PATH = "../data/npi_geocoded.json"
NEIGHBORHOODS_PATH = "../data/sf_neighborhoods.geojson"


def main():
    parser = argparse.ArgumentParser(description="Ingest NPI data into practices table")
    parser.add_argument("--data-path", default="../data/npi_full.csv", help="Path to NPI CSV file")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Re-filter the NPI CSV from scratch, ignoring any cached JSONL",
    )
    parser.add_argument(
        "--filter-only",
        action="store_true",
        help="Filter and transform only, skip geocoding/embedding/upsert",
    )
    args = parser.parse_args()

    if not args.full and os.path.exists(JSONL_PATH):
        print(f"Loading pre-filtered practices from {JSONL_PATH}...")
        practices = load_jsonl(JSONL_PATH)
        print(f"Loaded {len(practices)} practices.")
    else:
        print("Filtering NPI CSV...")
        practices = filter_and_transform(args.data_path)
        write_jsonl(practices, JSONL_PATH)
        print(f"Filtered {len(practices)} practices, saved to {JSONL_PATH}.")

    if args.filter_only:
        return

    print("Geocoding practices...")
    geocode_results = geocode_practices(practices, GEOCODE_CACHE_PATH)
    print("Loading SF neighborhoods...")
    neighborhoods = load_neighborhoods(NEIGHBORHOODS_PATH)
    enrich_with_neighborhoods(practices, geocode_results, neighborhoods)
    for practice in practices:
        practice["description"] = compose_description(practice)

    write_jsonl(practices, JSONL_PATH)
    print(f"Wrote enriched practices to {JSONL_PATH}.")

    conn = get_connection()
    already_embedded = fetch_embedded_npi_numbers(conn)
    pending = [p for p in practices if p["npi_number"] not in already_embedded]
    print(f"Embedding {len(pending)} practices ({len(already_embedded)} already done)...")
    embed_practices(pending, conn)
    conn.close()

    print(f"Ingested {len(pending)} practices ({len(already_embedded)} skipped).")


if __name__ == "__main__":
    main()
