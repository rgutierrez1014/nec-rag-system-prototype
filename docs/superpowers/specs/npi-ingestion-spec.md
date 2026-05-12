# NPI Ingestion

## Goal

Build the ingestion pipeline that filters the CMS National Provider Identifier (NPI) full replacement file to San Francisco County behavioral health providers, transforms each matching record into a Practice-shaped document, enriches each practice with its SF neighborhood name via reverse geocoding and point-in-polygon lookup, generates embeddings via Ollama, and upserts into the `practices` table. The output is a populated, queryable practices table — the seed dataset for all hybrid retrieval in the prototype.

## Architectural Context

**Practice-centric data model:** The primary searchable unit is a Practice, not a Professional. Each NPI record represents one licensed provider; in this prototype each provider maps to one Practice document. The transform creates a Practice-shaped document whose schema matches what a future production ingest from SRIP partner data will produce.

**Ingestion pattern:** Core ingestion logic lives in `api/ingestion/ingest_npi.py` as framework-agnostic Python functions that take a DB connection and config, do the work, and return a result. The FastAPI task endpoint (`/tasks/ingest-npi`) will be added in Step 4 as a thin HTTP trigger that calls these functions. For Step 2, the ingestion is invoked as a CLI script with a Makefile target. This matches the pattern established by the main NEC platform's tasks service (`compute_sensory_profiles.py`). No ORM — use `psycopg2` with `execute_values()` for bulk upserts.

**Object type registry:** The practices table is one of two searchable object types defined in `api/orchestration/registry.py`. The registry is a Python dict keyed by type name (`"practices"`, `"resources"`). Each entry specifies the table, embeddable fields, filterable metadata columns, and retrieval config. The NPI ingestion is the "populate" step for the `"practices"` type. The registry config for practices should be present or stubbed before ingest runs.

**Neighborhood enrichment:** Neighborhood names are embedded directly into the practice document text at ingestion time (not query time). This makes neighborhood names part of the corpus — both BM25 keyword matching and vector similarity handle queries like "OT in the Sunset" without special routing. Neighborhood is also stored as a structured metadata column for SQL filtering. The boundary data comes from SF's official neighborhood dataset (DataSF), loaded once and used for point-in-polygon lookups.

**Embedding model:** `nomic-embed-text` via Ollama, accessed at `localhost:11434` (via SSH tunnel in local dev). All embeddings are `vector(768)`. The model version string is stored as `embedding_model` on each row.

**Idempotency:** Upsert on `npi_number` as the primary key. Re-running the ingestion on already-ingested data produces no duplicate records and updates changed fields.

**pgvector cast:** Use `pgvector`'s psycopg2 adapter (`from pgvector.psycopg2 import register_vector`) to handle vector type registration transparently. Call `register_vector(conn)` after opening a connection. This is already a project dependency (`pgvector==0.3.*` in `requirements.txt`).

## Prerequisites / Prior Steps

Step 1 (Environment and vector store setup) must be complete:

- **`practices` table** must exist with the schema from `api/db/migrations/0001_initial.sql`. Confirmed present.
- **Ollama** with `nomic-embed-text` must be running on the VPS (accessible via SSH tunnel at `localhost:11434`). Verified by `make verify`.
- **DB connection** config (`DB_URL` or equivalent env vars) must be set in `.env`. Confirmed `.env` exists.
- **`api/ingestion/` directory** must exist. Confirmed present (empty `__init__.py`).
- **`shapely`** must be added to `requirements.txt` for point-in-polygon neighborhood lookups (pulls in GEOS C library bindings).

> Warning: The `practices` table is missing a `neighborhood` column. The spec requires it as a filterable metadata field for neighborhood-scoped SQL queries (e.g., `WHERE neighborhood = 'Sunset'`). Step 2 must add a migration (`api/db/migrations/0002_add_neighborhood.sql`) to add this column before or alongside the ingestion implementation. Checked: only `0001_initial.sql` exists.

## Scope

### 5.1 Add `neighborhood` migration

Create `api/db/migrations/0002_add_neighborhood.sql`:
- `ALTER TABLE practices ADD COLUMN IF NOT EXISTS neighborhood VARCHAR(100) NOT NULL DEFAULT '';`
- `CREATE INDEX IF NOT EXISTS practices_neighborhood_idx ON practices (neighborhood);`

Apply this migration to all three databases (dev, test, production) before running ingest.

### 5.2 NPI file filtering

The NPI full replacement file is a CSV available at `https://download.cms.gov/nppes/NPI_Files.html` (updated monthly by CMS). The file is large (~8GB uncompressed). The file is downloaded manually once and placed at a local path; the ingestion script does not download it.

**CSV parsing:** The NPI file is too large to load into memory. Stream the CSV using `csv.DictReader` or `pandas` chunked reading (`chunksize=10000`), filtering rows as they are read. This keeps memory bounded regardless of file size.

The ingestion script must:

- Accept the path to a local copy of the NPI CSV as a required CLI argument. Default: `data/npi_full.csv`.
- **Filter rows to behavioral health providers in San Francisco County:**
  - **Taxonomy codes:** Filter on `Healthcare Provider Taxonomy Code_1` (and optionally `_2` through `_15`) for the following prefixes:
    - `103T` — Psychologists
    - `101Y` — Counselors (licensed professional, mental health)
    - `106H` — Marriage & Family Therapists
    - `2084P` — Psychiatrists
    - `225X` — Occupational Therapists
    - `235Z` — Speech-Language Pathologists
    - `364S` — Psychiatric/Mental Health Nurse Practitioners
  - **County filter:** `Provider Business Practice Location Address State Name` = `CA` AND `Provider Business Practice Location Address City Name` = `SAN FRANCISCO` (case-insensitive). Note: the NPI column is named "State Name" but contains the two-letter state code (e.g., `CA`), not the full state name. Use as-is.
  - **Active records only:** `NPI Deactivation Date` is empty (blank field means the NPI is active).
  - **Organization vs individual:** Include both `NPI Type` 1 (individual) and 2 (organization). Type 1 providers are individual practitioners; they map to a Practice with a single-entry `professionals` roster. Type 2 are organizations; the professional roster may be empty initially (NPI data for organizations doesn't include staff lists).

- Log the count of rows matched.

### 5.3 Transform: NPI record → Practice document

Filter and transform happen in a single pass. Each matching NPI row is transformed into a Practice document dict and written to `data/npi_practices.json` (JSONL) as the canonical inspection artifact. The `description` field is composed later (after neighborhood enrichment in Section 5.4), so it is left empty at this stage.

| NPI field | Practice field | Notes |
|-----------|---------------|-------|
| `NPI` | `npi_number` | String, 10 digits |
| `Provider Organization Name (Legal Business Name)` or `Provider Last Name (Legal Name)` + `Provider First Name` | `name` | For Type 2: org name. For Type 1: "First Last, [Credential]" |
| Taxonomy code(s) | `services` | Map via taxonomy-to-service slug table (see below) |
| `Provider Business Practice Location Address First Line` | `address_1` | |
| `Provider Business Practice Location Address City Name` | `address_city` | Title-case ("SAN FRANCISCO" → "San Francisco") |
| `Provider Business Practice Location Address State Name` | `address_state` | Two-letter code, use as-is from NPI data |
| `Provider Business Practice Location Address Postal Code` | `address_zip` | Trim to 5 digits (NPI includes ZIP+4) |
| `Provider Credential Text` | `professionals[0].credential` | For Type 1 only |
| Taxonomy code description | `specialties` | Use the human-readable taxonomy description |

**`professionals` field:** JSON array. For Type 1 (individual), one entry: `{"name": "First Last", "credential": "LMFT"}`. For Type 2 (organization), empty array `[]`.

**`practice_type`:** Always `"healthcare"` for NPI-sourced records.

**`presence_types`:** Default to `["in-person"]`. NPI data does not include telehealth information.

**`specialties`:** Derive from the human-readable taxonomy description. Map the taxonomy description to specialty slugs matching the main platform's vocabulary (e.g., taxonomy description "Marriage & Family Therapist" → specialty slug `"marriage-family-therapy"`). This mapping table is defined in the ingestion config.

**Taxonomy-to-service slug mapping table** (define as a constant in the ingestion module):

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
```

This mapping is intentionally minimal for the prototype. A row may have multiple taxonomy codes — collect all matching slugs into the `services` array.

### 5.4 Neighborhood enrichment

Each practice's address must be enriched with its SF neighborhood before description composition and embedding.

**Step A — Address geocoding (address → lat/lon):**

The NPI data includes street addresses but not coordinates. Each address must be geocoded to obtain lat/lon for point-in-polygon lookup.

Use the **US Census Geocoding Service** (`https://geocoding.geo.census.gov/geocoder/`) — it is free, requires no API key, supports batch geocoding, and is appropriate for US addresses. The batch geocoder accepts a CSV of up to 10,000 addresses per call and returns lat/lon for each.

- Batch all filtered SF addresses in a single API call to the Census geocoder (expected ~500-2,000 records fits within the 10,000 limit).
- **Retry logic:** The Census geocoder can be slow and occasionally returns 5xx errors. Retry failed batch requests up to 3 times with exponential backoff.
- **Address normalization:** Strip suite/unit numbers (e.g., "Ste 200", "Unit B") from addresses before geocoding — the Census geocoder handles these poorly, and stripping them improves match rate.
- Handle non-matches (the Census geocoder does not match every address): for unmatched addresses, set `neighborhood = ""` and log the count. These records are still ingested — they just lack neighborhood enrichment.
- Cache geocoding results locally (`data/npi_geocoded.json`) keyed by `npi_number` so re-runs don't re-geocode. This is the only intermediate file that caches an expensive external API call.

**Step B — Point-in-polygon: lat/lon → SF neighborhood:**

San Francisco's official neighborhood boundary dataset is available from DataSF as a GeoJSON file:
- URL: `https://data.sfgov.org/resource/p5b7-5n3h.geojson` (SF Analysis Neighborhoods)
- This file is small (~500KB) and should be bundled in the repository at `data/sf_neighborhoods.geojson` (download once, commit to repo).

Use `shapely` for point-in-polygon lookup:
- Load the GeoJSON file once at the start of the enrichment step.
- For each geocoded practice, create a `shapely.geometry.Point(lon, lat)` and test against each neighborhood polygon to find the containing neighborhood.
- The `nhood` property in the SF DataSF GeoJSON is the neighborhood display name (e.g., "Sunset/Parkside", "Mission", "SoMa").
- Store the display name in the `neighborhood` column.

**Step C — Compose `description` and neighborhood location text:**

After neighborhood enrichment, compose the `description` field for each practice. The description includes the neighborhood when available, making it useful for both display and embedding:

For records with a neighborhood:
```
"{name} is a {taxonomy description} practice located in the {neighborhood} neighborhood of San Francisco ({address_1}, {address_zip})."
```

For records without a neighborhood (unmatched geocode):
```
"{name} is a {taxonomy description} practice located in San Francisco, CA ({address_1}, {address_zip})."
```

For records with multiple taxonomy codes, include all descriptions:
```
"{name} is a {taxonomy description 1} and {taxonomy description 2} practice located in..."
```

### 5.5 Embedding generation

For each enriched practice, construct the embedding input string by concatenating:
```
{description}. Services: {', '.join(services)}. Specialties: {', '.join(specialties)}. Professionals: {', '.join(p['name'] + ' ' + p.get('credential', '') for p in professionals)}.
```

The `description` field already contains name, specialty context, and neighborhood location text, so these are not repeated in the embedding input.

Call the Ollama API at `http://localhost:11434/api/embeddings` with `model: "nomic-embed-text"`. The response returns a `768`-dimensional float list.

Process in batches (e.g., 50 records at a time) to avoid memory pressure. Log progress every 100 records.

Store the Ollama model version string (obtain from `http://localhost:11434/api/tags` — use the `digest` field of the `nomic-embed-text` model) as `embedding_model` on each row.

### 5.6 Upsert into `practices` table

Use `psycopg2` with `execute_values()` for bulk upsert. Register the pgvector adapter on the connection before any insert:

```python
from pgvector.psycopg2 import register_vector
register_vector(conn)
```

Upsert on `npi_number`:

```sql
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
```

Commit in batches of 100 records. Log upserted vs skipped count after completion.

### 5.7 CLI entry point and Makefile target

The ingestion is invoked as a CLI script, not an HTTP endpoint. The FastAPI task endpoint (`POST /tasks/ingest-npi`) will be added in Step 4 when the FastAPI service skeleton is built.

**CLI entry point:** `python -m ingestion.ingest_npi --data-path data/npi_full.csv`

The script accepts:
- `--data-path` (required): path to the local NPI CSV file. Default: `data/npi_full.csv`.

**Makefile target:**

```makefile
ingest-npi:
	cd api && .venv/bin/python -m ingestion.ingest_npi --data-path ../data/npi_full.csv
```

The core ingestion functions remain framework-agnostic (take a DB connection and config, return a result) so the Step 4 HTTP trigger is a thin wrapper with no refactoring needed.

### 5.8 Verification

After ingestion, verify:
- Query record count: `SELECT COUNT(*) FROM practices;`
- Spot-check a sample: `SELECT name, address_city, neighborhood, services, embedding IS NOT NULL FROM practices LIMIT 10;`
- Verify neighborhood enrichment: `SELECT neighborhood, COUNT(*) FROM practices GROUP BY neighborhood ORDER BY COUNT(*) DESC;`

The existing `make verify` end-to-end check (Ollama → pgvector) continues to pass. Add a `make verify-npi` target that runs a quick DB query to confirm record count is non-zero.

## Acceptance Criteria

- The practices table contains NPI records filtered to SF County behavioral health providers (expected: ~500-2,000 records depending on current NPI data).
- Every row has a non-null `embedding` column (768-dimensional vector).
- Every row has a non-null `embedding_model` column identifying the nomic-embed-text version used.
- At least 80% of rows have a non-empty `neighborhood` column (geocoding success rate target).
- `services` arrays contain slugs from `TAXONOMY_TO_SERVICE` — no raw taxonomy codes in the services column.
- Re-running the ingestion produces no duplicate records (upsert idempotency).
- A spot-check of 5 random records shows correct field mapping: name, address, services match the source NPI data.
- `make ingest-npi` completes without error.
- The neighborhood distribution query shows recognizable SF neighborhood names with plausible counts (Sunset, Mission, SoMa, etc.).

## Out of Scope

- NPI file download — the file is downloaded manually and placed at a local path
- FastAPI task endpoint — deferred to Step 4 when the service skeleton is built
- Resource ingestion (Step 3) — handled separately in `ingest_resources.py`
- BM25 index construction (Step 5) — BM25 index is built in-memory when the retrieval layer starts; `/tasks/rebuild-bm25-index` is implemented in Step 5
- Hybrid retrieval, reranking, or any query-time logic (Step 5)
- PII preprocessing on ingested data — ingestion stores raw provider data; PII preprocessing applies at query time (Step 6)
- Insurance field — NPI data does not include insurance accepted; this field is left empty and noted as a SRIP data field
- Languages field — similarly absent from NPI data
- Telehealth/remote presence detection — NPI data does not include this; `presence_types` defaults to `["in-person"]` for all records
- Playwright or web scraping — that's Step 3 (resource ingestion)
- Automatic monthly NPI refresh — downloading and re-running the ingest script manually is sufficient for the prototype
