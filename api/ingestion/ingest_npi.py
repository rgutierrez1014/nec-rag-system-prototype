import argparse
import csv
import json
import os


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
        professionals = [{"name": f"{first} {last}", "credential": credential}]

    zip_code = row["Provider Business Practice Location Address Postal Code"].strip()[:5]

    return {
        "npi_number": row["NPI"].strip(),
        "name": name,
        "description": "",
        "practice_type": "healthcare",
        "address_1": row["Provider Business Practice Location Address First Line"].strip().title(),
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
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
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
    return practices


def write_jsonl(practices: list[dict], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True) if os.path.dirname(output_path) else None
    with open(output_path, "w", encoding="utf-8") as f:
        for practice in practices:
            f.write(json.dumps(practice) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Ingest NPI data into practices table")
    parser.add_argument("--data-path", default="../data/npi_full.csv", help="Path to NPI CSV file")
    parser.add_argument(
        "--filter-only",
        action="store_true",
        help="Filter and transform only, skip geocoding/embedding/upsert",
    )
    args = parser.parse_args()

    practices = filter_and_transform(args.data_path)

    if args.filter_only:
        write_jsonl(practices, "../data/npi_practices.jsonl")
        print(f"Wrote {len(practices)} practices to data/npi_practices.jsonl")
        return

    print(f"Filtered {len(practices)} practices. Geocoding, embedding, and upsert not yet implemented (Steps 3–4).")


if __name__ == "__main__":
    main()
