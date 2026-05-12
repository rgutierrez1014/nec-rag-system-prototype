# NPI Ingestion — Implementation Plan

## Goal

Build the ingestion pipeline that filters the CMS NPI full replacement file to San Francisco County behavioral health providers, transforms each matching record into a Practice document, enriches with SF neighborhood via geocoding + point-in-polygon, generates embeddings via Ollama, and upserts into the `practices` table. The output is a populated, queryable practices table — the seed dataset for all hybrid retrieval in the prototype.

## Context

This is Step 2 in the project's implementation order. Step 1 (environment + vector store setup) is complete: Postgres with pgvector is running on the VPS, the `practices` table exists, Ollama with nomic-embed-text is accessible via SSH tunnel, and `make verify` passes.

The ingestion is invoked as a CLI script (`make ingest-npi`), not an HTTP endpoint. The FastAPI task endpoint (`POST /tasks/ingest-npi`) is deferred to Step 4. Core functions are framework-agnostic — they take a DB connection and config, do the work, return a result — so the HTTP trigger will be a thin wrapper.

### Key architectural constraints

- **Practice-centric data model** — each NPI record maps to one Practice document.
- **No ORM** — `psycopg2` with `execute_values()` for bulk upserts.
- **Idempotent** — upsert on `npi_number` as conflict key.
- **nomic-embed-text requires prefixes** — `search_document:` for document embeddings, `search_query:` for query embeddings. The existing `verify_setup.py` already implements this correctly.
- **Specialties left empty** — NPI taxonomy codes map to services only. Specialties (`[]`) will be populated later from SRIP partner data, which carries sub-service granularity (techniques, equipment, modalities).

---

## Step 1: Prerequisites — dependencies, data directory, migration, shared embedding utility

Set up the foundation before the main ingestion work.

### 1a. Add `shapely` to requirements.txt

Add `shapely` for point-in-polygon neighborhood lookups:

**File:** `api/requirements.txt`

```
# Core
fastapi==0.115.*
uvicorn[standard]==0.34.*
psycopg2-binary==2.9.*
pgvector==0.3.*
httpx==0.28.*
yoyo-migrations==8.*

# Ingestion (Step 2)
shapely==2.*
```

Run `make setup-api` or `cd api && .venv/bin/pip install -r requirements.txt` to install.

### 1b. Create `data/` directory structure

```
mkdir -p data
```

The `data/` directory holds:
- `npi_full.csv` — the NPI full replacement file (manually downloaded, gitignored via `*.csv`)
- `sf_neighborhoods.geojson` — SF neighborhood boundaries (committed to repo)
- `npi_geocoded.json` — geocoding result cache (gitignored — regenerable)
- `npi_practices.jsonl` — filtered/transformed inspection artifact (gitignored — regenerable)

Add to `.gitignore`:
```
# NPI ingestion intermediate files (regenerable)
data/npi_geocoded.json
data/npi_practices.jsonl
```

The GeoJSON file is small (~500KB) and committed. CSV files are already gitignored (`*.csv`).

### 1c. Download SF neighborhood boundaries

Download the SF Analysis Neighborhoods GeoJSON from DataSF and commit to repo:

```bash
curl -o data/sf_neighborhoods.geojson \
  "https://data.sfgov.org/resource/p5b7-5n3h.geojson?\$limit=50000"
```

The `nhood` property in each feature is the neighborhood display name (e.g., "Sunset/Parkside", "Mission", "South of Market").

### 1d. Create neighborhood migration

**New file:** `api/db/migrations/0002_add_neighborhood.sql`

```sql
-- depends: 0001_initial

ALTER TABLE practices ADD COLUMN IF NOT EXISTS neighborhood VARCHAR(100) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS practices_neighborhood_idx ON practices (neighborhood);
```

Apply to dev database: `make apply-migrations` (or `make apply-migrations db=nec_rag_dev`).

### 1e. Extract shared embedding utility

Move the embedding generation logic from `verify_setup.py` into a shared module so both `verify_setup.py` and `ingest_npi.py` can use it.

**New file:** `api/embeddings.py`

```python
import os

import httpx

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


def get_embedding_model_version() -> str:
    response = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10.0)
    response.raise_for_status()
    for model in response.json().get("models", []):
        if model["name"].startswith(EMBEDDING_MODEL):
            return model.get("digest", EMBEDDING_MODEL)[:12]
    return EMBEDDING_MODEL
```

Update `api/scripts/verify_setup.py` to import from the shared module:

```python
from embeddings import generate_embedding, EMBEDDING_MODEL
```

Remove the duplicated `generate_embedding()`, `OLLAMA_BASE_URL`, and `EMBEDDING_MODEL` from `verify_setup.py`.

### 1f. Extract apply-migrations logic to a script (added during implementation)

The inline Python in the `apply-migrations` Makefile target was extracted to `api/scripts/apply_migrations.py`. The script applies pending migrations one at a time and prints per-migration status (Django-style output). The Makefile target is now a one-liner calling this script.

### Verification

1. `make setup-api` completes without error (shapely installs).
2. `make apply-migrations` adds the neighborhood column and prints per-migration status.
3. `data/sf_neighborhoods.geojson` exists and contains GeoJSON features with a neighborhood name property.
4. `make verify` still passes (verify_setup.py uses the shared embedding module).

### Deviations

**1c — Data source:** The plan specified the DataSF Analysis Neighborhoods dataset (`p5b7-5n3h`) with a `nhood` property per feature. That endpoint is broken: the SODA GeoJSON endpoint (`/resource/p5b7-5n3h.geojson`) returns 41 features with `"geometry": null` and `"properties": {}` (dataset registered in the catalog but no row data); the geospatial export endpoint (`/api/geospatial/p5b7-5n3h?method=export&type=GeoJSON`) returns a truncated 53-byte file despite a 200 response. Used the [codeforamerica/click_that_hood](https://github.com/codeforamerica/click_that_hood) SF GeoJSON instead — valid polygons for 37 SF neighborhoods, but the property name is `name` (not `nhood`). **The `load_neighborhoods()` function in Step 3 must use `feature["properties"]["name"]` instead of `feature["properties"]["nhood"]`.**

**1f — apply-migrations script:** The inline Python in the Makefile `apply-migrations` target was extracted to `api/scripts/apply_migrations.py` (not in the original plan). The script is self-contained (does not import from `setup_db.py`) and applies migrations one at a time within a single lock, printing Django-style per-migration status. The `api/scripts/apply_migrations.py` file should be added to the files summary.

---

## Step 2: NPI file filtering and Practice transform

Filter the NPI CSV to SF County behavioral health providers and transform each row into a Practice document dict.

### 2a. Core ingestion module

**New file:** `api/ingestion/ingest_npi.py`

This module contains all core ingestion logic as framework-agnostic functions. The CLI entry point is at the bottom (`if __name__ == "__main__"`).

**Taxonomy mapping constant:**

```python
TAXONOMY_TO_SERVICE = {
    "103T": "psychology",
    "101Y": "mental-health-counseling",
    "106H": "marriage-family-therapy",
    "2084P": "psychiatry",
    "225X": "occupational-therapy",
    "235Z": "speech-language-pathology",
    "364S": "psychiatric-nursing",
}

TAXONOMY_PREFIXES = tuple(TAXONOMY_TO_SERVICE.keys())
```

**CSV filtering — key NPI column names:**

| NPI CSV column | Used for |
|----------------|----------|
| `NPI` | Primary key (`npi_number`) |
| `Entity Type Code` | 1 = individual, 2 = organization |
| `Provider Organization Name (Legal Business Name)` | Org name (Type 2) |
| `Provider Last Name (Legal Name)` | Individual last name (Type 1) |
| `Provider First Name` | Individual first name (Type 1) |
| `Provider Credential Text` | Credential abbreviation (Type 1) |
| `Provider First Line Business Practice Location Address` | Street address |
| `Provider Business Practice Location Address City Name` | City (filter: SAN FRANCISCO) |
| `Provider Business Practice Location Address State Name` | State code (filter: CA) |
| `Provider Business Practice Location Address Postal Code` | ZIP+4 (trim to 5) |
| `Healthcare Provider Taxonomy Code_1` through `_15` | Taxonomy code matching |
| `NPI Deactivation Date` | Empty = active (filter) |

**Streaming filter logic:** Use `csv.DictReader` to stream the file. For each row:

1. Skip if `NPI Deactivation Date` is non-empty (deactivated).
2. Skip if city != `SAN FRANCISCO` (case-insensitive) or state != `CA`.
3. Collect all taxonomy codes from columns `Healthcare Provider Taxonomy Code_1` through `Healthcare Provider Taxonomy Code_15`.
4. Check if any taxonomy code starts with a prefix in `TAXONOMY_PREFIXES`. Skip if none match.
5. Transform matching row → Practice dict.

**Transform logic per row:**

```python
def transform_npi_row(row: dict) -> dict:
    entity_type = row["Entity Type Code"]
    taxonomy_codes = [
        row.get(f"Healthcare Provider Taxonomy Code_{i}", "")
        for i in range(1, 16)
    ]
    taxonomy_codes = [c for c in taxonomy_codes if c]

    services = list({
        slug for code in taxonomy_codes
        for prefix, slug in TAXONOMY_TO_SERVICE.items()
        if code.startswith(prefix)
    })

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
        "description": "",  # composed after neighborhood enrichment
        "practice_type": "healthcare",
        "address_1": row["Provider Business Practice Location Address First Line"].strip().title(),
        "address_city": "San Francisco",
        "address_state": "CA",
        "address_zip": zip_code,
        "neighborhood": "",  # enriched in Step 3
        "services": services,
        "specialties": [],
        "presence_types": ["in-person"],
        "professionals": professionals,
    }
```

**Write filtered practices to JSONL:** After filtering and transforming, write all practice dicts to `data/npi_practices.jsonl` (one JSON object per line) as the canonical inspection artifact. Descriptions are empty at this stage — they're composed after neighborhood enrichment.

### Verification

1. Run the filter on the NPI CSV: `cd api && .venv/bin/python -m ingestion.ingest_npi --data-path ../data/npi_full.csv --filter-only`
2. Check the JSONL output: `wc -l data/npi_practices.jsonl` — expect ~500-2,000 records.
3. Spot-check a few records: `head -5 data/npi_practices.jsonl | python -m json.tool`
4. Verify services arrays contain slugs from `TAXONOMY_TO_SERVICE`, not raw taxonomy codes.
5. Verify names are properly formatted (Title Case for individuals, org names).

### Deviations

**Column name (V2 NPPES file):** The street address column was renamed in the V2 file format. The plan specified `Provider Business Practice Location Address First Line`; the actual column is `Provider First Line Business Practice Location Address`. Updated in both `ingest_npi.py` and the plan's column reference table.

**Ordinal suffix fix:** `.title()` mangles ordinal suffixes in street numbers (24TH → 24Th). Added `_title_address()` helper using a regex to fix this (24Th → 24th).

**`credentials` field name:** The professionals dict uses `credentials` (plural) to match the NEC platform's field name, not `credential` as written in the plan.

**`npi_number` in professionals:** Added `npi_number` to the professionals dict for Type 1 records. For Type 1 NPI records, the NPI number belongs to the individual provider, so it's meaningful to carry it in the professionals entry for future deduplication when SRIP data links individuals to org practices.

**Expanded taxonomy types:** Four taxonomy types were added beyond the original plan (social work `1041`, developmental-behavioral pediatrics `2080P0006`, physical therapy `2251`, audiology `231H`). This raised the SF County record count from the estimated ~500–2,000 to ~12,775.

**Progress logging:** `filter_and_transform()` prints progress every 10,000 rows and a final total. Added during implementation to provide feedback while streaming the ~9GB CSV.

---

## Step 3: Neighborhood enrichment (geocoding + point-in-polygon)

Geocode filtered practice addresses and look up SF neighborhoods.

### 3a. Census Geocoder batch call

**Endpoint:** `https://geocoding.geo.census.gov/geocoder/geographies/addressbatch`

**Request format:** `POST` with `multipart/form-data`:
- `addressFile`: CSV file with columns: `Unique ID, Street Address, City, State, ZIP`
- `benchmark`: `Public_AR_Current`
- `vintage`: `Current_Current`
- `returntype`: `geographies`

**Response format:** CSV with columns including the matched coordinates (longitude, latitude) in columns at indices 5 and 6 for matched records, and a match indicator at index 2 (`Match` or `No_Match`).

**Implementation notes:**
- Batch all filtered practices in a single call (expected <2,000 records, well within the 10,000 limit).
- **Address normalization before geocoding:** Strip suite/unit numbers (regex: `r'\b(ste|suite|unit|apt|#)\s*\w+$'`, case-insensitive) — the Census geocoder handles these poorly.
- **Retry logic:** Retry failed requests up to 3 times with exponential backoff (2s, 4s, 8s). The Census geocoder can be slow and occasionally returns 5xx errors.
- **Cache results:** Write geocoding results to `data/npi_geocoded.json` keyed by `npi_number`. On re-run, load cache first and only geocode new/uncached NPIs.

```python
import io
import re
import time

def geocode_practices(practices: list[dict], cache_path: str) -> dict[str, tuple[float, float]]:
    """Batch geocode practices via Census Geocoder. Returns {npi_number: (lat, lon)}."""
    cache = load_json_cache(cache_path)

    uncached = [p for p in practices if p["npi_number"] not in cache]
    if not uncached:
        return cache

    csv_lines = []
    for p in uncached:
        address = strip_suite_number(p["address_1"])
        csv_lines.append(f'{p["npi_number"]},{address},{p["address_city"]},{p["address_state"]},{p["address_zip"]}')

    csv_content = "\n".join(csv_lines)
    results = call_census_batch_geocoder(csv_content, retries=3)
    cache.update(results)
    save_json_cache(cache_path, cache)
    return cache


def strip_suite_number(address: str) -> str:
    return re.sub(r'\b(ste|suite|unit|apt|#)\s*\S+$', '', address, flags=re.IGNORECASE).strip()
```

### 3b. Point-in-polygon neighborhood lookup

Use `shapely` to find the containing neighborhood for each geocoded point:

```python
import json
from shapely.geometry import shape, Point

def load_neighborhoods(geojson_path: str) -> list[tuple[str, any]]:
    """Load SF neighborhood polygons. Returns [(name, polygon), ...]."""
    with open(geojson_path) as f:
        data = json.load(f)
    neighborhoods = []
    for feature in data["features"]:
        name = feature["properties"]["nhood"]
        polygon = shape(feature["geometry"])
        neighborhoods.append((name, polygon))
    return neighborhoods


def lookup_neighborhood(lat: float, lon: float, neighborhoods: list) -> str:
    point = Point(lon, lat)
    for name, polygon in neighborhoods:
        if polygon.contains(point):
            return name
    return ""
```

### 3c. Compose description

After neighborhood enrichment, compose the `description` field:

```python
def compose_description(practice: dict) -> str:
    name = practice["name"]
    services_text = " and ".join(
        slug.replace("-", " ") for slug in practice["services"]
    )
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
```

### Verification

1. Check geocoding cache: `python -c "import json; d=json.load(open('data/npi_geocoded.json')); print(f'{len(d)} geocoded')"` — expect matches for 80%+ of records.
2. Check neighborhood distribution: count practices per neighborhood from the enriched data. Expect recognizable SF neighborhoods (Sunset/Parkside, Mission, SoMa, etc.).
3. Spot-check 5 descriptions: verify they include neighborhood names and correct address info.
4. Check for empty neighborhoods: count practices where `neighborhood == ""` — should be <20% of total.

### Deviations

**Refactor — geocoding and neighborhoods extracted to separate modules:** `geocode_practices` and helpers moved to `api/ingestion/geocoding.py`; `load_neighborhoods`, `lookup_neighborhood`, and `enrich_with_neighborhoods` moved to `api/ingestion/neighborhoods.py`. `compose_description` stayed in `ingest_npi.py`. A shared `api/http_utils.py` module was added with `http_post_with_retry` and `http_get_with_retry`; both `geocoding.py` and `embeddings.py` use it instead of bare `httpx` calls.

**Response format — coordinates are a single field:** The plan stated coordinates are at indices 5 and 6 as separate values. The actual Census Geocoder response puts them as a single `"lon,lat"` string in column 5, with column 6 being the TIGER/Line ID. Parser updated to split `row[5]` on comma.

**Batch size — chunking required:** The plan assumed <2,000 records and a single batch call. The actual filtered set is ~12,775 records, exceeding the Census Geocoder's 10,000-record limit. `geocode_practices` now chunks at 9,000 records per request.

**No-match caching:** The plan only cached matched coordinates. No-match responses were not cached, causing the 395 ungeocoded records to be re-submitted to the geocoder on every run. Updated `_call_census_batch_geocoder` to return `None` for no-match rows, and the cache now stores `null` for these entries so they are never re-submitted. `geocode_practices` returns only matched entries (non-null values) to callers.

**JSONL caching and `--full` flag:** The plan had no provision for skipping the slow CSV filtering step on retry. A load-from-JSONL fast path was added: if `data/npi_practices.jsonl` exists and `--full` is not passed, the script loads from it instead of re-filtering the CSV. `--full` forces a fresh filter. The JSONL is written immediately after filtering (before geocoding) so that a geocoding failure on first run doesn't require re-filtering on retry; it is overwritten again after enrichment with neighborhoods and descriptions populated.

---

## Step 4: Embedding generation and database upsert

Generate embeddings for all enriched practices and upsert into the `practices` table.

### 4a. Embedding generation

Use the shared `generate_embedding()` from `api/embeddings.py` (extracted in Step 1e).

**Embedding input string construction:**

```python
def build_embedding_input(practice: dict) -> str:
    parts = [practice["description"]]
    if practice["services"]:
        parts.append(f"Services: {', '.join(practice['services'])}")
    if practice["professionals"]:
        roster = ", ".join(
            f"{p['name']} {p.get('credential', '')}".strip()
            for p in practice["professionals"]
        )
        parts.append(f"Professionals: {roster}")
    return ". ".join(parts)
```

The `generate_embedding()` function handles the `search_document:` prefix automatically.

**Batching:** Process embeddings in batches of 50 records. Log progress every 100 records. Obtain the embedding model version string once at the start via `get_embedding_model_version()`.

### 4b. Database upsert

Register the pgvector adapter and upsert with `execute_values()`:

```python
from psycopg2.extras import execute_values, Json
from pgvector.psycopg2 import register_vector

def upsert_practices(conn, practices: list[dict]):
    register_vector(conn)
    cur = conn.cursor()

    values = [
        (
            p["npi_number"], p["name"], p["description"], p["practice_type"],
            p["address_1"], p["address_city"], p["address_state"], p["address_zip"],
            p["neighborhood"],
            p["services"], p["specialties"], p["presence_types"],
            Json(p["professionals"]),
            p["embedding"], p["embedding_model"],
        )
        for p in practices
    ]

    execute_values(
        cur,
        """
        INSERT INTO practices (
            npi_number, name, description, practice_type,
            address_1, address_city, address_state, address_zip,
            neighborhood,
            services, specialties, presence_types, professionals,
            embedding, embedding_model,
            updated_at
        ) VALUES %s
        ON CONFLICT (npi_number) DO UPDATE SET
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            address_1 = EXCLUDED.address_1,
            address_city = EXCLUDED.address_city,
            address_state = EXCLUDED.address_state,
            address_zip = EXCLUDED.address_zip,
            neighborhood = EXCLUDED.neighborhood,
            services = EXCLUDED.services,
            specialties = EXCLUDED.specialties,
            presence_types = EXCLUDED.presence_types,
            professionals = EXCLUDED.professionals,
            embedding = EXCLUDED.embedding,
            embedding_model = EXCLUDED.embedding_model,
            updated_at = NOW()
        """,
        values,
        page_size=100,
    )

    conn.commit()
    cur.close()
```

**Note on `updated_at`:** The `ON CONFLICT` clause sets `updated_at = NOW()` on update but the `INSERT` path relies on the column default (`DEFAULT NOW()`). The `updated_at` is not in the VALUES tuple — instead, append it to the SQL as a literal. Actually, since `execute_values` requires matching column count, add `NOW()` to the template:

Use the `template` parameter of `execute_values`:
```python
execute_values(
    cur,
    "INSERT INTO practices (..., updated_at) VALUES %s ON CONFLICT ...",
    values,
    template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
    page_size=100,
)
```

### 4c. CLI entry point and Makefile target

**CLI in `api/ingestion/ingest_npi.py`:**

```python
import argparse

def main():
    parser = argparse.ArgumentParser(description="Ingest NPI data into practices table")
    parser.add_argument("--data-path", default="../data/npi_full.csv", help="Path to NPI CSV file")
    parser.add_argument("--filter-only", action="store_true", help="Filter and transform only, skip geocoding/embedding/upsert")
    args = parser.parse_args()

    # 1. Filter + transform
    practices = filter_and_transform(args.data_path)

    if args.filter_only:
        write_jsonl(practices, "../data/npi_practices.jsonl")
        print(f"Wrote {len(practices)} practices to data/npi_practices.jsonl")
        return

    # 2. Geocode + neighborhood enrichment
    geocode_results = geocode_practices(practices, "../data/npi_geocoded.json")
    neighborhoods = load_neighborhoods("../data/sf_neighborhoods.geojson")
    enrich_with_neighborhoods(practices, geocode_results, neighborhoods)

    # 3. Compose descriptions
    for p in practices:
        p["description"] = compose_description(p)

    # 4. Write JSONL (with descriptions now populated)
    write_jsonl(practices, "../data/npi_practices.jsonl")

    # 5. Generate embeddings
    embed_practices(practices)

    # 6. Upsert to database
    conn = get_connection()
    upsert_practices(conn, practices)
    conn.close()

    print(f"Ingested {len(practices)} practices.")

if __name__ == "__main__":
    main()
```

**Makefile target:**

```makefile
ingest-npi:
	cd api && .venv/bin/python -m ingestion.ingest_npi --data-path ../data/npi_full.csv
```

### Verification

1. `make ingest-npi` completes without error.
2. Record count: `SELECT COUNT(*) FROM practices;` — expect ~500-2,000.
3. All embeddings present: `SELECT COUNT(*) FROM practices WHERE embedding IS NULL;` — expect 0.
4. All have embedding_model: `SELECT DISTINCT embedding_model FROM practices;` — expect one nomic-embed-text version string.
5. Neighborhood coverage: `SELECT COUNT(*) FROM practices WHERE neighborhood != '';` — expect 80%+ of total.
6. Neighborhood distribution: `SELECT neighborhood, COUNT(*) FROM practices GROUP BY neighborhood ORDER BY COUNT(*) DESC;` — expect recognizable SF neighborhoods.
7. Services check: `SELECT DISTINCT unnest(services) FROM practices;` — expect only slugs from `TAXONOMY_TO_SERVICE`.
8. Spot-check: `SELECT name, address_city, neighborhood, services, specialties FROM practices LIMIT 10;` — verify field mapping.
9. Idempotency: run `make ingest-npi` a second time, verify record count unchanged.
10. `make verify` still passes.

---

## Step 5: Verification targets

Add a `verify-npi` Makefile target for quick post-ingestion validation.

**Makefile addition:**

```makefile
verify-npi:
	@echo "Checking NPI ingestion results..."
	@cd api && .venv/bin/python -c "\
		from db.connection import get_connection; \
		conn = get_connection(); cur = conn.cursor(); \
		cur.execute('SELECT COUNT(*) FROM practices'); total = cur.fetchone()[0]; \
		cur.execute(\"SELECT COUNT(*) FROM practices WHERE neighborhood != ''\"); enriched = cur.fetchone()[0]; \
		cur.execute('SELECT COUNT(*) FROM practices WHERE embedding IS NOT NULL'); embedded = cur.fetchone()[0]; \
		print(f'  Total practices: {total}'); \
		print(f'  With neighborhood: {enriched} ({100*enriched//max(total,1)}%%)'); \
		print(f'  With embedding: {embedded}'); \
		cur.execute('SELECT neighborhood, COUNT(*) FROM practices GROUP BY neighborhood ORDER BY COUNT(*) DESC LIMIT 10'); \
		print('  Top neighborhoods:'); \
		[print(f'    {r[0] or \"(none)\"}: {r[1]}') for r in cur.fetchall()]; \
		conn.close()"
```

### Verification

1. `make verify-npi` prints counts and neighborhood distribution.
2. All numbers look reasonable per acceptance criteria.

---

## Files summary

| File | Action | Description |
|------|--------|-------------|
| `api/requirements.txt` | Modify | Add `shapely==2.*` |
| `api/db/migrations/0002_add_neighborhood.sql` | Create | Add `neighborhood` column + index to practices |
| `api/embeddings.py` | Create | Shared embedding generation utility (extracted from verify_setup.py) |
| `api/scripts/verify_setup.py` | Modify | Import `generate_embedding` from shared module |
| `api/ingestion/ingest_npi.py` | Create | Core ingestion logic: filter, transform, geocode, enrich, embed, upsert |
| `data/sf_neighborhoods.geojson` | Create | SF neighborhood boundaries from DataSF (committed) |
| `.gitignore` | Modify | Add `data/npi_geocoded.json` and `data/npi_practices.jsonl` |
| `Makefile` | Modify | Add `ingest-npi` and `verify-npi` targets |

---

## Acceptance criteria

- The practices table contains NPI records filtered to SF County behavioral health providers (expected: ~500-2,000 records).
- Every row has a non-null `embedding` column (768-dimensional vector).
- Every row has a non-null `embedding_model` column identifying the nomic-embed-text version used.
- At least 80% of rows have a non-empty `neighborhood` column.
- `services` arrays contain slugs from `TAXONOMY_TO_SERVICE` — no raw taxonomy codes.
- `specialties` arrays are empty (`[]`) for all NPI-sourced records.
- Re-running the ingestion produces no duplicate records (upsert idempotency).
- `make ingest-npi` completes without error.
- `make verify-npi` shows recognizable SF neighborhood names with plausible counts.
- `make verify` continues to pass after extracting the shared embedding module.

---

## Completion checklist

- [x] Step 1: Prerequisites — shapely dependency, data directory, neighborhood migration, shared embedding utility
- [x] Step 2: NPI file filtering and Practice transform
- [x] Step 3: Neighborhood enrichment (geocoding + point-in-polygon)
- [ ] Step 4: Embedding generation and database upsert (includes CLI + Makefile)
- [ ] Step 5: Verification targets
