# Three-Database Isolation + yoyo Migrations — Implementation Plan

## Context and Goals

The original environment setup plan shares a single Postgres database (`nec_rag`) between local development and production. There is no test database — tests would hit the same instance as everything else. This plan introduces three logically isolated databases on the same VPS Postgres instance, managed by yoyo-migrations, eliminating data contamination risk.

### Database Layout

| Database | Used by | Created by |
|---|---|---|
| `nec_rag` | Production (deployed container) | `make setup-db` (via SSH tunnel) |
| `nec_rag_dev` | Local development | `make setup-db` (via SSH tunnel) |
| `nec_rag_test` | Test suite | pytest conftest (per test run) |

- Local `.env` uses `POSTGRES_DB=nec_rag_dev`
- Production env (configured in Dokploy UI) uses `POSTGRES_DB=nec_rag`
- `nec_rag_test` is never in any `.env` — pytest hardcodes it and manages its full lifecycle

### Why yoyo-migrations

The project uses raw psycopg2 with no ORM. yoyo accepts plain SQL files, has no SQLAlchemy dependency, and is lightweight. Alembic would pull in SQLAlchemy for no benefit. Migration path to Alembic is straightforward later if needed.

---

## Prerequisites

- SSH tunnel to VPS is functional (`make tunnel`)
- Python venv exists with dev dependencies (`make setup-api`)
- Postgres is running on VPS via docker-compose (`docker compose up -d`)

---

## Step 1: Add yoyo-migrations and Create Initial Migration

**What:** Add yoyo-migrations to dependencies, create the migrations directory, and write the initial schema as a migration file (replacing the planned `api/db/schema.sql`).

**Why:** The migration file becomes the single source of truth for schema. The docker-compose init mount of `schema.sql` is removed — databases are initialized via `make setup-db` instead.

### Files to create

**`api/db/migrations/0001_initial.sql`**
```sql
-- depends:

CREATE EXTENSION IF NOT EXISTS vector;

-- Practices: one row per provider practice (from NPI data)
CREATE TABLE IF NOT EXISTS practices (
    id SERIAL PRIMARY KEY,
    npi_number VARCHAR(10) UNIQUE NOT NULL,

    -- Practice identity
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    practice_type VARCHAR(20) NOT NULL DEFAULT 'healthcare',

    -- Address
    address_1 TEXT NOT NULL DEFAULT '',
    address_city VARCHAR(100) NOT NULL DEFAULT '',
    address_state VARCHAR(2) NOT NULL DEFAULT '',
    address_zip VARCHAR(10) NOT NULL DEFAULT '',

    -- Structured metadata (filterable)
    services TEXT[] NOT NULL DEFAULT '{}',
    specialties TEXT[] NOT NULL DEFAULT '{}',
    presence_types TEXT[] NOT NULL DEFAULT '{}',

    -- Professional roster
    professionals JSONB NOT NULL DEFAULT '[]',

    -- Embedding
    embedding vector(768),
    embedding_model VARCHAR(100),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for vector similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS practices_embedding_idx
    ON practices USING hnsw (embedding vector_cosine_ops);

-- Filter indexes
CREATE INDEX IF NOT EXISTS practices_zip_idx ON practices (address_zip);
CREATE INDEX IF NOT EXISTS practices_services_idx ON practices USING gin (services);
CREATE INDEX IF NOT EXISTS practices_specialties_idx ON practices USING gin (specialties);


-- Resource chunks: one row per chunk of a curated resource page
CREATE TABLE IF NOT EXISTS resource_chunks (
    id SERIAL PRIMARY KEY,
    content_hash VARCHAR(64) UNIQUE NOT NULL,

    -- Source metadata
    source_url TEXT NOT NULL,
    org_name TEXT NOT NULL DEFAULT '',
    page_title TEXT NOT NULL DEFAULT '',
    county_scope VARCHAR(50) NOT NULL DEFAULT 'national',
    fetch_date DATE,

    -- Content
    content TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    section_header TEXT NOT NULL DEFAULT '',

    -- Embedding
    embedding vector(768),
    embedding_model VARCHAR(100),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for vector similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS resource_chunks_embedding_idx
    ON resource_chunks USING hnsw (embedding vector_cosine_ops);

-- Lookup indexes
CREATE INDEX IF NOT EXISTS resource_chunks_source_url_idx ON resource_chunks (source_url);
CREATE INDEX IF NOT EXISTS resource_chunks_county_scope_idx ON resource_chunks (county_scope);
```

The `-- depends:` header is yoyo's dependency declaration format. For the first migration, it has no dependencies.

### Files to modify

**`api/requirements.txt`** — add `yoyo-migrations==8.*` to the Core section:
```
# Core
fastapi==0.115.*
uvicorn[standard]==0.34.*
psycopg2-binary==2.9.*
pgvector==0.3.*
httpx==0.28.*
yoyo-migrations==8.*
```

**`docker-compose.yml`** — remove the schema.sql init mount:
```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-nec_rag}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB:-nec_rag}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-nec_rag}"]
      interval: 5s
      timeout: 5s
      retries: 5

  dozzle:
    image: amir20/dozzle:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:9999:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro

volumes:
  pgdata:
```

**`.env.example`** — change `POSTGRES_DB` default to `nec_rag_dev`:
```
POSTGRES_DB=nec_rag_dev
```

### Verification

- [ ] `cd api && .venv/bin/pip install -r requirements.txt` installs yoyo-migrations without errors
- [ ] `api/db/migrations/0001_initial.sql` exists with valid SQL
- [ ] `docker-compose.yml` has no reference to `schema.sql`
- [ ] `.env.example` shows `POSTGRES_DB=nec_rag_dev`

---

## Step 2: Database Setup Script and Makefile Targets

**What:** Create a Python script (`api/scripts/setup_db.py`) that creates the dev and production databases (if they don't exist) and applies all pending migrations via yoyo's Python API. Add `setup-db` and `apply-migrations` Makefile targets.

**Why:** A Python script avoids requiring `psql` to be installed locally. It connects to the `postgres` default database to issue CREATE DATABASE, then applies yoyo migrations to each target database.

### Files to create

**`api/scripts/setup_db.py`**
```python
"""
Create dev and production databases (if they don't exist) and apply all
pending yoyo migrations to both.

Usage:
    make setup-db

Requires SSH tunnel to VPS (make tunnel).
"""

import os
import sys

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from yoyo import get_backend, read_migrations


MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "db", "migrations")

POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")

DATABASES = ["nec_rag", "nec_rag_dev"]


def create_database_if_not_exists(dbname):
    conn = psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname="postgres",
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    if not cur.fetchone():
        cur.execute(f'CREATE DATABASE "{dbname}"')
        print(f"  Created database: {dbname}")
    else:
        print(f"  Database already exists: {dbname}")
    cur.close()
    conn.close()


def apply_migrations(dbname):
    url = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{dbname}"
    backend = get_backend(url)
    migrations = read_migrations(MIGRATIONS_DIR)
    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))
    print(f"  Migrations applied to: {dbname}")


def main():
    print("Setting up databases...")
    for dbname in DATABASES:
        create_database_if_not_exists(dbname)

    print("\nApplying migrations...")
    for dbname in DATABASES:
        apply_migrations(dbname)

    print("\nDone.")


if __name__ == "__main__":
    main()
```

**`api/scripts/__init__.py`** — empty (if not already present)

### Files to modify

**`Makefile`** — add `setup-db` and `apply-migrations` targets, update the `.PHONY` line and the `test` target:

Add to `.PHONY`:
```makefile
.PHONY: start-dev stop-dev logs tunnel api frontend status setup-api setup-db apply-migrations verify test
```

Add after the `setup-api` target:
```makefile
setup-db:
	@echo "Requires SSH tunnel (make tunnel). Creating databases and applying migrations..."
	cd api && .venv/bin/python -m scripts.setup_db

apply-migrations:
	@echo "Applying pending migrations to $(POSTGRES_DB)..."
	cd api && .venv/bin/python -c "\
		import os; from yoyo import get_backend, read_migrations; \
		url = 'postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)'; \
		backend = get_backend(url); migrations = read_migrations('db/migrations'); \
		backend.lock(); backend.apply_migrations(backend.to_apply(migrations)); \
		print('Migrations applied to $(POSTGRES_DB)')"
```

Update the `test` target to print a tunnel reminder:
```makefile
test:
	@echo "Requires SSH tunnel (make tunnel)."
	cd api && .venv/bin/pytest tests/ -v
```

### Verification

- [ ] `make tunnel` then `make setup-db` creates both databases and applies migrations
- [ ] Connect to `nec_rag_dev`: `psql -h localhost -U nec_rag -d nec_rag_dev -c "\dt"` shows both tables
- [ ] Connect to `nec_rag`: `psql -h localhost -U nec_rag -d nec_rag -c "\dt"` shows both tables
- [ ] `make apply-migrations` is idempotent (running again shows no errors)

---

## Step 3: Test Database Lifecycle (conftest.py)

**What:** Create `api/tests/conftest.py` with a session-scoped fixture that creates/drops `nec_rag_test` and a function-scoped `db_conn` fixture with rollback isolation.

**Why:** Tests get a clean database every session (Django-style lifecycle). Per-test rollback means tests don't interfere with each other. The `skipif` guard pattern from the old plan is unnecessary — if the tunnel isn't open, conftest fails fast with a connection error.

### Files to create

**`api/tests/conftest.py`**
```python
import os

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from pgvector.psycopg2 import register_vector
from yoyo import get_backend, read_migrations
import pytest


POSTGRES_USER = os.environ.get("POSTGRES_USER", "nec_rag")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "localdev")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
TEST_DB = "nec_rag_test"

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "db", "migrations")


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create nec_rag_test, apply migrations, yield, then drop it."""
    conn = psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname="postgres",
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    cur.execute(f"CREATE DATABASE {TEST_DB}")
    cur.close()
    conn.close()

    url = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{TEST_DB}"
    backend = get_backend(url)
    migrations = read_migrations(MIGRATIONS_DIR)
    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))

    yield

    conn = psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname="postgres",
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    cur.close()
    conn.close()


@pytest.fixture
def db_conn():
    """Per-test connection to nec_rag_test. Rolls back on teardown."""
    conn = psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=TEST_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )
    register_vector(conn)
    yield conn
    conn.rollback()
    conn.close()
```

### Files to create

**`api/tests/test_verify_setup.py`**
```python
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
        SELECT id, name, 1 - (embedding <=> %s) AS similarity
        FROM practices
        WHERE id = %s
        """,
        (fake_embedding, record_id),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[2] == pytest.approx(1.0, abs=0.001)
```

Note: The `db_conn` fixture is defined in `conftest.py` and automatically available to all test files. No `pytestmark = pytest.mark.skipif(...)` — if Postgres is unreachable, conftest fails fast with a clear connection error.

### Verification

- [ ] `make tunnel` then `make test` passes all tests
- [ ] During the test run, `nec_rag_test` exists temporarily (can verify via `psql -h localhost -U nec_rag -c "\l"` while tests are running)
- [ ] After tests complete, `nec_rag_test` is gone (verify via `\l`)
- [ ] Running `make test` a second time still passes (clean slate each run)

---

## Step 4: End-to-End Verification Script

**What:** Create the verify_setup.py script that tests Ollama embedding generation and pgvector round-trip.

**Why:** This is the acceptance test for the full infrastructure stack. It confirms your Mac can reach both Ollama and Postgres on the VPS through the SSH tunnel.

### Files to create

**`api/scripts/verify_setup.py`**
```python
"""
End-to-end verification: generate an embedding via Ollama, write it to
pgvector, query it back via similarity search.

Usage:
    make verify

Requires POSTGRES_* and OLLAMA_BASE_URL env vars (see .env.example).
"""

import os
import sys

import httpx
import psycopg2
from pgvector.psycopg2 import register_vector

from db.connection import get_connection


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


def main():
    print("1. Connecting to Postgres...")
    conn = get_connection()
    register_vector(conn)
    cur = conn.cursor()

    print("2. Verifying pgvector extension...")
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    if not row:
        print("FAIL: pgvector extension not installed")
        sys.exit(1)
    print(f"   pgvector version: {row[0]}")

    print("3. Generating test embedding via Ollama...")
    test_text = "Occupational therapy practice in San Francisco specializing in sensory integration"
    embedding = generate_embedding(test_text)
    dim = len(embedding)
    print(f"   Embedding dimension: {dim}")
    if dim != 768:
        print(f"FAIL: Expected 768 dimensions, got {dim}")
        sys.exit(1)

    print("4. Inserting test practice record...")
    cur.execute(
        """
        INSERT INTO practices (npi_number, name, description, address_city, address_state,
                               address_zip, services, embedding, embedding_model)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (npi_number) DO UPDATE SET embedding = EXCLUDED.embedding
        RETURNING id
        """,
        (
            "0000000001",
            "Test OT Practice",
            test_text,
            "San Francisco",
            "CA",
            "94110",
            ["occupational-therapy"],
            embedding,
            EMBEDDING_MODEL,
        ),
    )
    record_id = cur.fetchone()[0]
    conn.commit()
    print(f"   Inserted practice id={record_id}")

    print("5. Querying via similarity search...")
    query_text = "OT for kids with sensory issues"
    query_embedding = generate_embedding(query_text, prefix="search_query")
    cur.execute(
        """
        SELECT id, name, 1 - (embedding <=> %s) AS similarity
        FROM practices
        ORDER BY embedding <=> %s
        LIMIT 5
        """,
        (query_embedding, query_embedding),
    )
    results = cur.fetchall()
    print(f"   Found {len(results)} results:")
    for row in results:
        print(f"     id={row[0]} name={row[1]!r} similarity={row[2]:.4f}")

    print("6. Cleaning up test data...")
    cur.execute("DELETE FROM practices WHERE npi_number = '0000000001'")
    conn.commit()

    cur.close()
    conn.close()

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
```

### Verification

- [ ] `make tunnel` then `make verify` completes with "All checks passed"
- [ ] The script inserts a record, queries it, and cleans up — no test data left behind

---

## Step 5: Update Existing Plan and Deploy docker-compose

**What:** Deploy the updated docker-compose.yml to the VPS (without the schema.sql mount) and reset the database so it's initialized via `make setup-db` instead. Update the existing plan file's completion checklist.

**Why:** The VPS Postgres currently may have been initialized with the old approach (or not at all). This step ensures the VPS state matches the new migration-based approach.

### Instructions

1. SSH into the VPS and pull latest changes (or copy the updated `docker-compose.yml`)
2. Reset the database volume to start fresh:
   ```bash
   docker compose down -v
   docker compose up -d
   ```
3. From your local Mac, with the tunnel open:
   ```bash
   make setup-db
   ```
   This creates both `nec_rag` and `nec_rag_dev` and applies the initial migration to both.

4. Update `.env` locally: change `POSTGRES_DB=nec_rag` to `POSTGRES_DB=nec_rag_dev`

### Verification

- [ ] `docker compose up -d` on VPS starts Postgres without schema.sql mount
- [ ] `make setup-db` from local Mac creates both databases
- [ ] `make verify` passes (confirms Ollama + pgvector pipeline with dev database)
- [ ] `make test` passes (confirms test database lifecycle)

---

## Impact on Future Steps

- **Any step that adds new tables or columns:** Add a numbered migration file (e.g., `0002_add_search_logs.sql`) rather than modifying `0001_initial.sql`.
- **Deployment step:** Will add `api/entrypoint.sh` that runs `yoyo apply` before starting uvicorn. yoyo is idempotent — safe to run on every container start.
- **`.env.production`:** Deferred to deployment step. Dokploy UI is the primary config source; the file is kept as reference.

---

## Files Summary

| File | Status | Description |
|------|--------|-------------|
| `api/db/migrations/0001_initial.sql` | New | Initial schema (practices + resource_chunks tables, HNSW indexes) |
| `api/scripts/setup_db.py` | New | Creates databases and applies yoyo migrations |
| `api/scripts/__init__.py` | New | Empty package init |
| `api/scripts/verify_setup.py` | New | End-to-end Ollama → pgvector round-trip verification |
| `api/tests/conftest.py` | New | Session fixture for test DB lifecycle + `db_conn` fixture |
| `api/tests/test_verify_setup.py` | New | Integration tests for schema and vector operations |
| `api/requirements.txt` | Modified | Add `yoyo-migrations==8.*` |
| `.env.example` | Modified | `POSTGRES_DB=nec_rag_dev` (was `nec_rag`) |
| `Makefile` | Modified | Add `setup-db`, `apply-migrations` targets; update `test` with tunnel reminder |
| `docker-compose.yml` | Modified | Remove `schema.sql` init mount from `db` service |

---

## Completion Checklist

- [x] Step 1: Add yoyo-migrations and create initial migration
- [x] Step 2: Database setup script and Makefile targets
- [x] Step 3: Test database lifecycle (conftest.py)
- [x] Step 4: End-to-end verification script
- [x] Step 5: Deploy updated docker-compose and initialize databases

## Deviations from plan (discovered during implementation)

- **`::vector` cast required on similarity queries** — psycopg2 sends Python lists as `numeric[]`, not `vector`, so the `<=>` operator fails without an explicit `::vector` cast on the parameter. Added `%s::vector` in `verify_setup.py` and `test_verify_setup.py`. This will apply to all similarity queries in future steps.
- **`pg_terminate_backend` required before DROP DATABASE in conftest teardown** — yoyo holds a connection open after applying migrations. Without terminating active sessions first, `DROP DATABASE IF EXISTS nec_rag_test` fails with "database is being accessed by other users". Added a `pg_terminate_backend` call targeting `nec_rag_test` before the drop.
