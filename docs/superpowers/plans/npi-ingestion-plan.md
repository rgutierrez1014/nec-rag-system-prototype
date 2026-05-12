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
- **nomic-embed-text requires prefixes** — `search_document:` for document embeddings, `search_query:` for query embeddings. The existing `verify/verify_infra.py` already implements this correctly.
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

Download the SF neighborhoods GeoJSON from [codeforamerica/click_that_hood](https://github.com/codeforamerica/click_that_hood) and commit to repo. This source provides valid polygons for 37 SF neighborhoods with a `name` property per feature (e.g., "Sunset/Parkside", "Mission", "South of Market").

Note: The DataSF Analysis Neighborhoods SODA endpoint (`/resource/p5b7-5n3h.geojson`) is broken — it returns features with null geometries. Use the click_that_hood source instead.

### 1d. Create neighborhood migration

**New file:** `api/db/migrations/0002_add_neighborhood.sql`

```sql
-- depends: 0001_initial

ALTER TABLE practices ADD COLUMN IF NOT EXISTS neighborhood VARCHAR(100) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS practices_neighborhood_idx ON practices (neighborhood);
```

Apply to dev database: `make apply-migrations` (or `make apply-migrations db=nec_rag_dev`).

### 1e. Extract shared HTTP and embedding utilities

**New file:** `api/http_utils.py`

Extract retry logic into a shared module used by both `embeddings.py` and `ingestion/geocoding.py`:

```python
def http_post_with_retry(url, *, retries=3, backoff=2, **kwargs) -> httpx.Response:
    ...

def http_get_with_retry(url, *, retries=3, backoff=2, **kwargs) -> httpx.Response:
    ...
```

**New file:** `api/embeddings.py`

```python
import os
from http_utils import http_get_with_retry, http_post_with_retry

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")


def generate_embedding(text: str, prefix: str = "search_document") -> list[float]:
    response = http_post_with_retry(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        timeout=30.0,
        json={"model": EMBEDDING_MODEL, "prompt": f"{prefix}: {text}"},
    )
    return response.json()["embedding"]


def get_embedding_model_version() -> str:
    response = http_get_with_retry(f"{OLLAMA_BASE_URL}/api/tags", timeout=10.0)
    for model in response.json().get("models", []):
        if model["name"].startswith(EMBEDDING_MODEL):
            return model.get("digest", EMBEDDING_MODEL)[:12]
    return EMBEDDING_MODEL
```

Update `api/verify/verify_infra.py` to import from the shared module:

```python
from embeddings import generate_embedding, EMBEDDING_MODEL
```

### 1f. Extract apply-migrations logic to a script

Extract the inline Python in the `apply-migrations` Makefile target to `api/scripts/apply_migrations.py`. The script applies pending migrations one at a time within a single lock and prints per-migration status (Django-style output). The Makefile target becomes a one-liner calling this script.

### Verification

1. `make setup-api` completes without error (shapely installs).
2. `make apply-migrations` adds the neighborhood column and prints per-migration status.
3. `data/sf_neighborhoods.geojson` exists and contains GeoJSON features with a `name` property.
4. `make verify` still passes (verify_infra.py uses the shared embedding module).

---

## Step 2: NPI file filtering and Practice transform

Filter the NPI CSV to SF County behavioral health providers and transform each row into a Practice document dict.

### 2a. Core ingestion module

**New file:** `api/ingestion/ingest_npi.py`

This module contains the filter, transform, compose-description, and CLI entry point. Geocoding, neighborhood enrichment, embedding, and upsert each live in their own submodule (see Steps 3 and 4).

**Taxonomy mapping constant** — includes behavioral health and related allied health disciplines. ABA is intentionally excluded per SPEC.md guardrails:

```python
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
}

TAXONOMY_PREFIXES = tuple(TAXONOMY_TO_SERVICE.keys())
```

**CSV filtering — key NPI column names (V2 NPPES format):**

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

Log progress every 10,000 rows — the NPI CSV is ~9GB and feedback during streaming is essential.

**Address title-casing helper:** `.title()` mangles ordinal suffixes in street numbers (24TH → 24Th). Use a regex helper to fix this:

```python
_ORDINAL_RE = re.compile(r'(\d+)(St|Nd|Rd|Th)\b')

def _title_address(address: str) -> str:
    return _ORDINAL_RE.sub(lambda m: m.group(1) + m.group(2).lower(), address.title())
```

**Transform logic per row:**

```python
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
        # npi_number carried in professionals for future deduplication when SRIP data
        # links individuals to org practices
        professionals = [{"name": f"{first} {last}", "credentials": credential, "npi_number": row["NPI"].strip()}]

    zip_code = row["Provider Business Practice Location Address Postal Code"].strip()[:5]

    return {
        "npi_number": row["NPI"].strip(),
        "name": name,
        "description": "",  # composed after neighborhood enrichment
        "practice_type": "healthcare",
        "address_1": _title_address(row["Provider First Line Business Practice Location Address"].strip()),
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

Note: the professionals dict uses `credentials` (plural) to match the NEC platform's field name.

**Write filtered practices to JSONL:** After filtering and transforming, write all practice dicts to `data/npi_practices.jsonl` (one JSON object per line) as the canonical inspection artifact. Descriptions are empty at this stage — they're composed after neighborhood enrichment.

**JSONL fast-path:** If `data/npi_practices.jsonl` already exists, load from it instead of re-filtering the CSV. Pass `--full` to force a fresh filter. This is essential since the NPI CSV takes several minutes to stream.

### Verification

1. Run the filter on the NPI CSV: `cd api && .venv/bin/python -m ingestion.ingest_npi --data-path ../data/npi_full.csv --filter-only`
2. Check the JSONL output: `wc -l data/npi_practices.jsonl` — expect ~12,000+ records.
3. Spot-check a few records: `head -5 data/npi_practices.jsonl | python -m json.tool`
4. Verify services arrays contain slugs from `TAXONOMY_TO_SERVICE`, not raw taxonomy codes.
5. Verify names are properly formatted (Title Case for individuals, org names).

---

## Step 3: Neighborhood enrichment (geocoding + point-in-polygon)

Geocode filtered practice addresses and look up SF neighborhoods. Each concern lives in its own module: `api/ingestion/geocoding.py` and `api/ingestion/neighborhoods.py`.

### 3a. Census Geocoder batch call

**New file:** `api/ingestion/geocoding.py`

**Endpoint:** `https://geocoding.geo.census.gov/geocoder/geographies/addressbatch`

**Request format:** `POST` with `multipart/form-data`:
- `addressFile`: CSV file with columns: `Unique ID, Street Address, City, State, ZIP`
- `benchmark`: `Public_AR_Current`
- `vintage`: `Current_Current`
- `returntype`: `geographies`

**Response format:** CSV where matched records have a `"lon,lat"` string at index 5 (a single comma-separated field, not two separate columns). Index 2 is the match indicator (`Match` or `No_Match`).

**Implementation notes:**
- Chunk requests at 9,000 records (the filtered SF set is ~12,775, exceeding the 10,000-record limit).
- **Address normalization before geocoding:** Strip suite/unit numbers (regex: `r'\b(ste|suite|unit|apt|#)\s*\w+$'`, case-insensitive) — the Census geocoder handles these poorly.
- **Retry logic:** Use `http_post_with_retry` from `api/http_utils.py` — the Census geocoder can be slow and occasionally returns 5xx errors.
- **Cache results:** Write geocoding results to `data/npi_geocoded.json` keyed by `npi_number`. On re-run, load cache first and only geocode new/uncached NPIs. Cache `null` for no-match entries so they are never re-submitted.

```python
def geocode_practices(practices: list[dict], cache_path: str) -> dict[str, tuple[float, float]]:
    """Batch geocode practices via Census Geocoder. Returns {npi_number: (lat, lon)} for matched records."""
    cache = load_json_cache(cache_path)

    uncached = [p for p in practices if p["npi_number"] not in cache]
    if not uncached:
        return {k: v for k, v in cache.items() if v is not None}

    for chunk in chunks(uncached, 9000):
        results = _call_census_batch_geocoder(chunk)
        cache.update(results)  # includes null entries for no-match

    save_json_cache(cache_path, cache)
    return {k: v for k, v in cache.items() if v is not None}
```

### 3b. Point-in-polygon neighborhood lookup

**New file:** `api/ingestion/neighborhoods.py`

Use `shapely` to find the containing neighborhood for each geocoded point. The GeoJSON uses `feature["properties"]["name"]` for the neighborhood display name:

```python
from shapely.geometry import shape, Point

def load_neighborhoods(geojson_path: str) -> list[tuple[str, any]]:
    """Load SF neighborhood polygons. Returns [(name, polygon), ...]."""
    with open(geojson_path) as f:
        data = json.load(f)
    return [
        (feature["properties"]["name"], shape(feature["geometry"]))
        for feature in data["features"]
    ]


def lookup_neighborhood(lat: float, lon: float, neighborhoods: list) -> str:
    point = Point(lon, lat)
    for name, polygon in neighborhoods:
        if polygon.contains(point):
            return name
    return ""
```

### 3c. Compose description

After neighborhood enrichment, compose the `description` field in `ingest_npi.py`:

```python
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
```

### Verification

1. Check geocoding cache: `python -c "import json; d=json.load(open('data/npi_geocoded.json')); print(f'{sum(1 for v in d.values() if v)} matched, {sum(1 for v in d.values() if not v)} no-match')"` — expect matches for 80%+ of records.
2. Check neighborhood distribution: count practices per neighborhood from the enriched data. Expect recognizable SF neighborhoods (Sunset/Parkside, Mission, SoMa, etc.).
3. Spot-check 5 descriptions: verify they include neighborhood names and correct address info.
4. Check for empty neighborhoods: count practices where `neighborhood == ""` — should be <20% of total.

---

## Step 4: Embedding generation and database upsert

Generate embeddings for all enriched practices and upsert into the `practices` table. Each concern lives in its own module to match the pattern established in Step 3.

### 4a. Embedding generation module

**New file:** `api/ingestion/embedding.py`

```python
from embeddings import generate_embedding, get_embedding_model_version
from ingestion.upsert import upsert_practices


def build_embedding_input(practice: dict) -> str:
    parts = [practice["description"]]
    if practice["services"]:
        parts.append(f"Services: {', '.join(practice['services'])}")
    if practice["professionals"]:
        roster = ", ".join(
            f"{p['name']} {p.get('credentials', '')}".strip()
            for p in practice["professionals"]
        )
        parts.append(f"Professionals: {roster}")
    return ". ".join(parts)


def embed_practices(practices: list[dict], conn) -> None:
    model_version = get_embedding_model_version()
    total = len(practices)
    for i in range(0, total, 50):
        batch = practices[i:i + 50]
        for practice in batch:
            practice["embedding"] = generate_embedding(build_embedding_input(practice))
            practice["embedding_model"] = model_version
        upsert_practices(conn, batch)
        done = min(i + 50, total)
        if done % 100 == 0 or done == total:
            print(f"  Embedded and upserted {done}/{total} practices.")
```

Each batch of 50 is upserted immediately after embedding. This makes the pipeline resumable — if interrupted, a restart skips already-embedded NPIs and picks up where it left off (see 4b).

### 4b. Database upsert module

**New file:** `api/ingestion/upsert.py`

```python
from psycopg2.extras import Json, execute_values
from pgvector.psycopg2 import register_vector


def fetch_embedded_npi_numbers(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT npi_number FROM practices WHERE embedding IS NOT NULL")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result


def upsert_practices(conn, practices: list[dict]) -> None:
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
        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        page_size=100,
    )

    conn.commit()
    cur.close()
```

### 4c. CLI entry point and Makefile target

**CLI in `api/ingestion/ingest_npi.py`:**

```python
def main():
    parser = argparse.ArgumentParser(description="Ingest NPI data into practices table")
    parser.add_argument("--data-path", default="../data/npi_full.csv")
    parser.add_argument("--full", action="store_true", help="Re-filter CSV from scratch")
    parser.add_argument("--filter-only", action="store_true", help="Skip geocoding/embedding/upsert")
    args = parser.parse_args()

    if not args.full and os.path.exists(JSONL_PATH):
        practices = load_jsonl(JSONL_PATH)
    else:
        practices = filter_and_transform(args.data_path)
        write_jsonl(practices, JSONL_PATH)

    if args.filter_only:
        return

    geocode_results = geocode_practices(practices, GEOCODE_CACHE_PATH)
    neighborhoods = load_neighborhoods(NEIGHBORHOODS_PATH)
    enrich_with_neighborhoods(practices, geocode_results, neighborhoods)
    for practice in practices:
        practice["description"] = compose_description(practice)
    write_jsonl(practices, JSONL_PATH)

    conn = get_connection()
    already_embedded = fetch_embedded_npi_numbers(conn)
    pending = [p for p in practices if p["npi_number"] not in already_embedded]
    print(f"Embedding {len(pending)} practices ({len(already_embedded)} already done)...")
    embed_practices(pending, conn)
    conn.close()
```

**Makefile target:**

```makefile
ingest-npi:
    @echo "Requires SSH tunnel (make tunnel)."
    cd api && .venv/bin/python -m ingestion.ingest_npi --data-path ../data/npi_full.csv
```

### Verification

Run `make verify-npi` (see Step 5). Additionally:

1. `make ingest-npi` completes without error.
2. Idempotency: run `make ingest-npi` a second time, verify record count unchanged.
3. `make verify` still passes.

---

## Step 5: Verification targets

Verification scripts live in `api/verify/` — separate from `api/tests/`, which is for unit and integration tests against the test database. The `verify/` convention is for scripts that validate a running system against `nec_rag_dev`.

### 5a. Infrastructure verification

**File:** `api/verify/verify_infra.py`

Pytest-based end-to-end check of all infrastructure components. Runs against `nec_rag_dev`:

- Postgres connection
- pgvector extension installed
- Ollama returns 768-dimensional embeddings
- Embedding roundtrip: insert a test practice, query by similarity, verify similarity > 0.3, clean up

Note: nomic-embed-text similarity scores for related-but-differently-worded sentences typically land in the 0.4–0.5 range, so 0.3 is the appropriate floor for this sanity check.

**Makefile target:**

```makefile
verify-infra:
    @echo "Requires SSH tunnel (make tunnel)."
    cd api && .venv/bin/pytest verify/verify_infra.py -v
```

### 5b. NPI ingestion verification

**File:** `api/verify/npi_ingestion.py`

Pytest-based post-ingestion validation against `nec_rag_dev`. Checks:

- Practices exist
- All rows have embeddings (768-dimensional) and a consistent `embedding_model`
- Services contain only valid slugs from `TAXONOMY_TO_SERVICE`
- Specialties are empty for all NPI rows
- 80%+ neighborhood coverage
- All rows have `address_city = 'San Francisco'` and `address_state = 'CA'`
- No duplicate NPI numbers
- No empty names

**Makefile target:**

```makefile
verify-npi:
    @echo "Requires SSH tunnel (make tunnel)."
    cd api && .venv/bin/pytest verify/npi_ingestion.py -v
```

### 5c. Combined verify target

`make verify` runs all verification scripts in sequence:

```makefile
verify: verify-infra verify-npi
```

### 5d. Unit and integration tests for ingestion functions

**File:** `api/tests/test_ingest_npi.py`

Tests run against `nec_rag_test` (the disposable test database created and dropped by the existing `conftest.py` fixture). Covers:

- `_title_address` — ordinal suffix correction
- `transform_npi_row` — individual provider, organization, multi-taxonomy, zip trimming
- `filter_and_transform` — exclusion rules (deactivated, wrong city, no matching taxonomy)
- `compose_description` — with and without neighborhood
- `build_embedding_input` — description + services + professionals
- `embed_practices` — mocked Ollama; verifies embedding and model version are attached and upsert is called per batch
- `upsert_practices` / `fetch_embedded_npi_numbers` — real DB via `db_conn` fixture; idempotency and field updates

### Verification

1. `make verify-infra` passes (all infra components reachable).
2. `make verify-npi` passes (all ingestion acceptance criteria met).
3. `make test` passes (unit and integration tests against nec_rag_test).

---

## Files summary

| File | Action | Description |
|------|--------|-------------|
| `api/requirements.txt` | Modify | Add `shapely==2.*` |
| `api/db/migrations/0002_add_neighborhood.sql` | Create | Add `neighborhood` column + index to practices |
| `api/http_utils.py` | Create | Shared HTTP retry utilities used by embeddings and geocoding |
| `api/embeddings.py` | Create | Shared embedding generation utility |
| `api/scripts/apply_migrations.py` | Create | Migration runner extracted from Makefile inline Python |
| `api/ingestion/ingest_npi.py` | Create | Filter, transform, compose description, CLI entry point |
| `api/ingestion/geocoding.py` | Create | Census Geocoder batch call and cache |
| `api/ingestion/neighborhoods.py` | Create | SF neighborhood polygon load and point-in-polygon lookup |
| `api/ingestion/embedding.py` | Create | Embedding input construction and per-batch embed+upsert |
| `api/ingestion/upsert.py` | Create | `upsert_practices` and `fetch_embedded_npi_numbers` |
| `api/verify/verify_infra.py` | Create | End-to-end infrastructure verification (pytest) |
| `api/verify/npi_ingestion.py` | Create | Post-ingestion data verification against nec_rag_dev (pytest) |
| `api/tests/test_ingest_npi.py` | Create | Unit and integration tests for ingestion functions |
| `data/sf_neighborhoods.geojson` | Create | SF neighborhood boundaries from codeforamerica/click_that_hood (committed) |
| `.gitignore` | Modify | Add `data/npi_geocoded.json` and `data/npi_practices.jsonl` |
| `Makefile` | Modify | Add `ingest-npi`, `verify-infra`, `verify-npi` targets; `verify` runs both |

---

## Acceptance criteria

- The practices table contains NPI records filtered to SF County behavioral health providers (~12,775 records).
- Every row has a non-null `embedding` column (768-dimensional vector).
- Every row has a non-null `embedding_model` column identifying the nomic-embed-text version used.
- At least 80% of rows have a non-empty `neighborhood` column.
- `services` arrays contain slugs from `TAXONOMY_TO_SERVICE` — no raw taxonomy codes.
- `specialties` arrays are empty (`[]`) for all NPI-sourced records.
- Re-running the ingestion produces no duplicate records (upsert idempotency).
- Interrupting and restarting the ingestion resumes from the last completed batch.
- `make ingest-npi` completes without error.
- `make verify` passes (infra + npi checks).
- `make test` passes.

---

## Completion checklist

- [x] Step 1: Prerequisites — shapely dependency, data directory, neighborhood migration, shared embedding utility
- [x] Step 2: NPI file filtering and Practice transform
- [x] Step 3: Neighborhood enrichment (geocoding + point-in-polygon)
- [x] Step 4: Embedding generation and database upsert (includes CLI + Makefile)
- [x] Step 5: Verification targets
