# NPI Ingestion

## Goal

Build the ingestion pipeline that downloads the CMS National Provider Identifier (NPI) full replacement file, filters it to San Francisco County behavioral health providers, transforms each matching record into a Practice-shaped document, enriches each practice with its SF neighborhood name via reverse geocoding and point-in-polygon lookup, generates embeddings via Ollama, and upserts into the `practices` table. The output is a populated, queryable practices table — the seed dataset for all hybrid retrieval in the prototype.

## Architectural Context

**Practice-centric data model:** The primary searchable unit is a Practice, not a Professional. Each NPI record represents one licensed provider; in this prototype each provider maps to one Practice document. The transform creates a Practice-shaped document whose schema matches what a future production ingest from SRIP partner data will produce.

**Ingestion pattern:** Core ingestion logic lives in `api/ingestion/ingest_npi.py` as framework-agnostic Python functions that take a DB connection and config, do the work, and return a result. The FastAPI task endpoint (`/tasks/ingest-npi`) is a thin HTTP trigger that calls these functions. This matches the pattern established by the main NEC platform's tasks service (`compute_sensory_profiles.py`). No ORM — use `psycopg2` with `execute_values()` for bulk upserts.

**Object type registry:** The practices table is one of two searchable object types defined in `api/orchestration/registry.py`. The registry is a Python dict keyed by type name (`"practices"`, `"resources"`). Each entry specifies the table, embeddable fields, filterable metadata columns, and retrieval config. The NPI ingestion is the "populate" step for the `"practices"` type. The registry config for practices should be present or stubbed before ingest runs.

**Neighborhood enrichment:** Neighborhood names are embedded directly into the practice document text at ingestion time (not query time). This makes neighborhood names part of the corpus — both BM25 keyword matching and vector similarity handle queries like "OT in the Sunset" without special routing. Neighborhood is also stored as a structured metadata column for SQL filtering. The boundary data comes from SF's official neighborhood dataset (DataSF), loaded once and used for point-in-polygon lookups.

**Embedding model:** `nomic-embed-text` via Ollama, accessed at `localhost:11434` (via SSH tunnel in local dev). All embeddings are `vector(768)`. The model version string is stored as `embedding_model` on each row.

**Idempotency:** Upsert on `npi_number` as the primary key. Re-running the ingestion on already-ingested data produces no duplicate records and updates changed fields.

**pgvector cast:** psycopg2 sends Python lists as `numeric[]`, not `vector`. Always cast embedding parameters explicitly: `%s::vector`. This applies to all `<=>` similarity queries and upsert inserts.

## Prerequisites / Prior Steps

Step 1 (Environment and vector store setup) must be complete:

- **`practices` table** must exist with the schema from `api/db/migrations/0001_initial.sql`. ✅ Confirmed present.
- **Ollama** with `nomic-embed-text` must be running on the VPS (accessible via SSH tunnel at `localhost:11434`). Verified by `make verify`.
- **DB connection** config (`DB_URL` or equivalent env vars) must be set in `.env`. ✅ Confirmed `.env` exists.
- **`api/ingestion/` directory** must exist. ✅ Confirmed present (empty `__init__.py`).

> ⚠ Warning: The `practices` table is missing a `neighborhood` column. The spec requires it as a filterable metadata field for neighborhood-scoped SQL queries (e.g., `WHERE neighborhood = 'Sunset'`). Step 2 must add a migration (`api/db/migrations/0002_add_neighborhood.sql`) to add this column before or alongside the ingestion implementation. Checked: only `0001_initial.sql` exists.

## Scope

### 5.1 Add `neighborhood` migration

Create `api/db/migrations/0002_add_neighborhood.sql`:
- `ALTER TABLE practices ADD COLUMN IF NOT EXISTS neighborhood VARCHAR(100) NOT NULL DEFAULT '';`
- `CREATE INDEX IF NOT EXISTS practices_neighborhood_idx ON practices (neighborhood);`

Apply this migration to all three databases (dev, test, production) before running ingest.

### 5.2 NPI file download and filtering

The NPI full replacement file is a CSV available at `https://download.cms.gov/nppes/NPI_Files.html` (updated monthly by CMS). The file is large (~8GB uncompressed). The ingestion script must:

- Accept the path to a local copy of the NPI CSV as a parameter (don't re-download on every run; the file is downloaded once).
- **Alternatively**, check whether a local copy exists and download only if absent. The download URL follows a predictable pattern but CMS occasionally changes it — implement as an optional step with clear logging.
- **Filter rows to behavioral health providers in San Francisco County:**
  - **Taxonomy codes:** Filter on `Healthcare Provider Taxonomy Code_1` (and optionally `_2` through `_15`) for the following prefixes:
    - `103T` — Psychologists
    - `101Y` — Counselors (licensed professional, mental health)
    - `106H` — Marriage & Family Therapists
    - `2084P` — Psychiatrists
    - `225X` — Occupational Therapists
    - `235Z` — Speech-Language Pathologists
    - `364S` — Psychiatric/Mental Health Nurse Practitioners
  - **County filter:** `Provider Business Practice Location Address State Name` = `CA` AND `Provider Business Practice Location Address City Name` = `SAN FRANCISCO` (case-insensitive). Note: NPI data uses all-caps city names.
  - **Active records only:** `NPI Deactivation Date` is empty (blank field means the NPI is active).
  - **Organization vs individual:** Include both `NPI Type` 1 (individual) and 2 (organization). Type 1 providers are individual practitioners; they map to a Practice with a single-entry `professionals` roster. Type 2 are organizations; the professional roster may be empty initially (NPI data for organizations doesn't include staff lists).

- Write filtered rows to a local intermediate JSON file (`data/npi_filtered.json`) for inspection before embedding. This file is a list of raw NPI row dicts, one per line (JSONL format). Log the count of rows matched.

### 5.3 Transform: NPI record → Practice document

Map each filtered NPI row to a Practice document dict:

| NPI field | Practice field | Notes |
|-----------|---------------|-------|
| `NPI` | `npi_number` | String, 10 digits |
| `Provider Organization Name (Legal Business Name)` or `Provider Last Name (Legal Name)` + `Provider First Name` | `name` | For Type 2: org name. For Type 1: "First Last, [Credential]" |
| Taxonomy code(s) | `services` | Map via taxonomy-to-service slug table (see below) |
| `Provider Business Practice Location Address First Line` | `address_1` | |
| `Provider Business Practice Location Address City Name` | `address_city` | Title-case ("SAN FRANCISCO" → "San Francisco") |
| `Provider Business Practice Location Address State Name` | `address_state` | Two-letter code from full state name |
| `Provider Business Practice Location Address Postal Code` | `address_zip` | Trim to 5 digits (NPI includes ZIP+4) |
| `Provider Credential Text` | `professionals[0].credential` | For Type 1 only |
| Taxonomy code description | `specialties` | Use the human-readable taxonomy description |

**`description` field:** Compose from available fields: `"{name} is a {taxonomy description} practice located in {city}, {state}."` This is the primary text for embedding quality — make it informative.

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

Write the transformed records to `data/npi_transformed.json` (JSONL) before neighborhood enrichment and embedding. This file is the canonical inspection artifact.

### 5.4 Neighborhood enrichment

Each practice's address must be enriched with its SF neighborhood before embedding.

**Step A — Address geocoding (address → lat/lon):**

The NPI data includes street addresses but not coordinates. Each address must be geocoded to obtain lat/lon for point-in-polygon lookup.

Use the **US Census Geocoding Service** (`https://geocoding.geo.census.gov/geocoder/`) — it is free, requires no API key, supports batch geocoding, and is appropriate for US addresses. The batch geocoder accepts a CSV of up to 10,000 addresses and returns lat/lon for each.

- Batch all filtered SF addresses in a single API call to the Census geocoder.
- Handle non-matches (the Census geocoder does not match every address): for unmatched addresses, set `neighborhood = ""` and log the count. These records are still ingested — they just lack neighborhood enrichment.
- Cache geocoding results locally (`data/npi_geocoded.json`) keyed by `npi_number` so re-runs don't re-geocode.

**Step B — Point-in-polygon: lat/lon → SF neighborhood:**

San Francisco's official neighborhood boundary dataset is available from DataSF as a GeoJSON file:
- URL: `https://data.sfgov.org/resource/p5b7-5n3h.geojson` (SF Analysis Neighborhoods)
- This file is small (~500KB) and should be bundled in the repository at `data/sf_neighborhoods.geojson` (download once, commit to repo).

Use `shapely` for point-in-polygon lookup:
- Load the GeoJSON file once at the start of the enrichment step.
- For each geocoded practice, create a `shapely.geometry.Point(lon, lat)` and test against each neighborhood polygon to find the containing neighborhood.
- The `nhood` property in the SF DataSF GeoJSON is the neighborhood display name (e.g., "Sunset/Parkside", "Mission", "SoMa").
- Store the display name in the `neighborhood` column.

**Embedding text construction with neighborhood:**

Instead of embedding a plain address, use natural language:
```
"located in the {neighborhood} neighborhood of San Francisco ({address_1}, {address_zip})"
```
If `neighborhood` is empty (unmatched geocode), fall back to:
```
"located in San Francisco, CA ({address_1}, {address_zip})"
```

### 5.5 Embedding generation

For each transformed+enriched practice, construct the embedding input string by concatenating:
```
{name}. {description}. Services: {', '.join(services)}. Specialties: {', '.join(specialties)}. {neighborhood_text}. Professionals: {', '.join(p['name'] + ' ' + p.get('credential', '') for p in professionals)}.
```

Call the Ollama API at `http://localhost:11434/api/embeddings` with `model: "nomic-embed-text"`. The response returns a `768`-dimensional float list.

Process in batches (e.g., 50 records at a time) to avoid memory pressure. Log progress every 100 records.

Store the Ollama model version string (obtain from `http://localhost:11434/api/tags` — use the `digest` field of the `nomic-embed-text` model) as `embedding_model` on each row.

### 5.6 Upsert into `practices` table

Use `psycopg2` with `execute_values()` for bulk upsert. Upsert on `npi_number`:

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

**pgvector cast:** Pass embedding as a Python list; psycopg2 requires explicit cast: the embedding column value in the VALUES tuple must be formatted as a string `'[0.1, 0.2, ...]'` or use `pgvector`'s `register_vector()` adapter. Prefer the explicit string cast approach: `str(embedding_list)` and the SQL column as `embedding = EXCLUDED.embedding::vector`. Alternatively, use `pgvector`'s `psycopg2` adapter (`from pgvector.psycopg2 import register_vector`) which handles the cast transparently.

Commit in batches of 100 records. Log upserted vs skipped count after completion.

### 5.7 FastAPI task endpoint

Register the ingestion function as a task endpoint in `api/tasks/router.py`:

```
POST /tasks/ingest-npi
```

The endpoint triggers the ingestion pipeline asynchronously (background task). Returns `{"status": "started"}` immediately. The ingestion function logs progress; check logs to monitor completion.

Endpoint accepts an optional `data_path` query parameter to specify the path to the local NPI CSV file. Default: `data/npi_full.csv`.

### 5.8 Verification

After ingestion, verify:
- Query record count: `SELECT COUNT(*) FROM practices;`
- Spot-check a sample: `SELECT name, address_city, neighborhood, services, embedding IS NOT NULL FROM practices LIMIT 10;`
- Verify neighborhood enrichment: `SELECT neighborhood, COUNT(*) FROM practices GROUP BY neighborhood ORDER BY COUNT(*) DESC;`

The existing `make verify` end-to-end check (Ollama → pgvector) continues to pass. Add a `make verify-npi` target that runs a quick DB query to confirm record count is non-zero.

## Acceptance Criteria

- The practices table contains NPI records filtered to SF County behavioral health providers (expected: ~500–2,000 records depending on current NPI data).
- Every row has a non-null `embedding` column (768-dimensional vector).
- Every row has a non-null `embedding_model` column identifying the nomic-embed-text version used.
- At least 80% of rows have a non-empty `neighborhood` column (geocoding success rate target).
- `services` arrays contain slugs from `TAXONOMY_TO_SERVICE` — no raw taxonomy codes in the services column.
- Re-running the ingestion produces no duplicate records (upsert idempotency).
- A spot-check of 5 random records shows correct field mapping: name, address, services match the source NPI data.
- `POST /tasks/ingest-npi` returns `{"status": "started"}` and the pipeline completes without error (verify via logs).
- The neighborhood distribution query shows recognizable SF neighborhood names with plausible counts (Sunset, Mission, SoMa, etc.).

## Out of Scope

- Resource ingestion (Step 3) — handled separately in `ingest_resources.py`
- BM25 index construction (Step 5) — BM25 index is built in-memory when the retrieval layer starts; `/tasks/rebuild-bm25-index` is implemented in Step 5
- Hybrid retrieval, reranking, or any query-time logic (Step 5)
- PII preprocessing on ingested data — ingestion stores raw provider data; PII preprocessing applies at query time (Step 6)
- Insurance field — NPI data does not include insurance accepted; this field is left empty and noted as a SRIP data field
- Languages field — similarly absent from NPI data
- Telehealth/remote presence detection — NPI data does not include this; `presence_types` defaults to `["in-person"]` for all records
- Playwright or web scraping — that's Step 3 (resource ingestion)
- Automatic monthly NPI refresh — downloading and re-running the ingest script manually is sufficient for the prototype
