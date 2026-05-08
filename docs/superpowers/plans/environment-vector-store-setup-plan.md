# Step 1: Environment and Vector Store Setup — Implementation Plan

## Context and Goals

Set up the foundational infrastructure for the NEC RAG search service: Postgres + pgvector and Ollama running on a Hetzner VPS, a monorepo with separate `api/` and `frontend/` directories, and a Makefile that wires everything together for local development. By the end of this step, you can run `make start-dev`, generate an embedding via Ollama on the VPS, write it to pgvector, and read it back — confirming the full embedding-to-storage pipeline works end-to-end.

This step produces no application logic. It creates the skeleton that Steps 2-9 build on.

### Development model

- **VPS hosts the heavy, stateless services:** Postgres + pgvector and Ollama run on the Hetzner CX33 permanently. These are infrastructure — they exist once and serve both development and production.
- **Local MacBook runs the code:** The FastAPI app, ingestion scripts, and tests run locally in a Python venv, connecting to VPS services via SSH tunnel. The Next.js frontend runs locally via `npm run dev` (added in Step 9).
- **Deployment via Dokploy:** Push to the production branch. Dokploy watches the branch, builds and deploys the FastAPI container on the VPS. The deployed container connects to the same Postgres and Ollama — but via localhost instead of over the tunnel.

This avoids duplicating resource-heavy services and keeps the 2017 MacBook (16GB RAM, quad-core i7) focused on running Python code, not ML models and databases.

### Monorepo structure

```
nec-rag-system-prototype/
├── api/                        # FastAPI service
│   ├── db/
│   │   ├── __init__.py
│   │   ├── connection.py
│   │   └── schema.sql
│   ├── ingestion/
│   │   └── __init__.py
│   ├── orchestration/
│   │   └── __init__.py
│   ├── tasks/
│   │   └── __init__.py
│   ├── scripts/
│   │   └── verify_setup.py
│   ├── tests/
│   │   ├── __init__.py
│   │   └── test_verify_setup.py
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── .venv/                  (gitignored)
├── frontend/                   # Next.js demo client (Step 9)
│   ├── package.json
│   └── node_modules/           (gitignored)
├── docker-compose.yml          # VPS services (Postgres)
├── Makefile
├── .env.example
├── .env                        (gitignored)
├── .gitignore
├── CLAUDE.md
├── LICENSE
├── README.md
└── docs/
```

## Prerequisites

- Python 3.11+ installed locally
- A Hetzner account (for VPS provisioning)
- SSH key pair for VPS access
- Docker and Docker Compose on the VPS (installed by Dokploy)

## Architectural Context

**Tech choices that constrain this step:**

- **Postgres + pgvector** — the `pgvector/pgvector:pg16` Docker image provides PostgreSQL 16 with the pgvector extension pre-installed. PostgreSQL 16 was chosen for its Cloud SQL support runway (extended support through Feb 2029, deprecation Feb 2032).
- **Ollama + nomic-embed-text** — self-hosted embedding model producing 768-dimensional vectors. Used for both ingestion and query-time embedding. The model uses a prefix convention: `search_document:` for documents being embedded, `search_query:` for queries at retrieval time.
- **HNSW indexes** — pgvector's Hierarchical Navigable Small World index for approximate nearest neighbor search. One index per object type table. Uses cosine distance operator (`vector_cosine_ops`).
- **psycopg2** — direct SQL via psycopg2, no ORM. Follows the NEC platform tasks service pattern.
- **Embedding model version tracking** — the model version string is stored alongside every vector. If the model is upgraded, all vectors must be re-embedded (mixing model versions produces incorrect similarity results).

**Data model relevant to schema design:**

Two tables in prototype scope:

1. `practices` — one row per Practice (from NPI data). Fields: name, description, practice_type, address fields, services (text array), specialties (text array), presence_types (text array), professional roster (JSONB), NPI number (unique key), embedding vector, embedding model version.
2. `resource_chunks` — one row per chunk of a curated resource page. Fields: source URL, org name, page title, content, chunk index, content hash (unique key for idempotent upserts), county scope, fetch date, embedding vector, embedding model version.

---

## Step 1: Provision Hetzner VPS and Configure Dokploy

**What:** Provision the Hetzner CX33 VPS, install Dokploy, wire up the Cloudflare origin certificate to Traefik, set up Ollama, and configure SSH access. This is manual infrastructure work done once.

**Why:** The VPS hosts all the heavy services (Postgres, Ollama) permanently. Both local development and deployed production connect to these same services. Getting the VPS running first means everything else has infrastructure to talk to.

Two subdomains are exposed from this VPS:
- `search.ndequity.org` — the RAG API (and demo frontend in Step 9)
- `dokploy-search.ndequity.org` — the Dokploy dashboard

Both use the existing wildcard Cloudflare origin certificate (`*.ndequity.org`). See `docs/traefik-cloudflare-cert-setup.md` for the full cert wiring guide — the steps below summarize the relevant parts.

### Instructions

1. **Create a Hetzner CX33 instance:**
   - Location: choose a US or EU datacenter (nearest to you)
   - OS: Ubuntu 22.04 LTS
   - Specs: 4 Intel vCPU, 8GB RAM, 80GB SSD ($8.59/month)
   - Add your SSH public key during creation

2. **SSH into the VPS and install Dokploy:**
   ```bash
   ssh root@<vps-ip>
   curl -sSL https://dokploy.com/install.sh | sh
   ```

3. **Access Dokploy dashboard and configure the project:**
   - Navigate to `http://<vps-ip>:3000` in your browser
   - Create your admin account
   - In **Settings → General**, set the Dokploy dashboard domain to `dokploy-search.ndequity.org`
   - Create a project for the RAG service
   - Set up Dokploy to watch the `production` branch of the repository

4. **Configure Cloudflare DNS:**
   - Add A records for both `search.ndequity.org` and `dokploy-search.ndequity.org` pointing to the VPS IP
   - Enable Cloudflare proxy (orange cloud) on both records
   - SSL mode: **Full (strict)** — the origin cert handles this end-to-end

5. **Wire up the Cloudflare origin certificate to Traefik:**

   a. In **Dokploy UI → Settings → Certificates**, upload the existing `*.ndequity.org` Cloudflare origin cert (PEM + private key).

   b. SSH into the VPS and find the cert filenames Dokploy created:
   ```bash
   ls -la /etc/dokploy/traefik/dynamic/certificates/
   ```

   c. In **Dokploy UI → Advanced → Traefik**, verify the static config has a file provider:
   ```yaml
   providers:
     file:
       directory: /etc/dokploy/traefik/dynamic
       watch: true
   ```
   Add it if missing and restart the Traefik container from the UI.

   d. On the VPS, create `/etc/dokploy/traefik/dynamic/default-cert.yml`:
   ```yaml
   tls:
     stores:
       default:
         defaultCertificate:
           certFile: /etc/dokploy/traefik/dynamic/certificates/<your-cert-filename>.crt
           keyFile: /etc/dokploy/traefik/dynamic/certificates/<your-cert-filename>.key
   ```
   Traefik picks this up automatically — no restart needed.

   e. Verify the cert is live:
   ```bash
   curl -I https://dokploy-search.ndequity.org
   ```
   The issuer should be "Cloudflare Inc ECC CA-3", not "TRAEFIK DEFAULT CERT".

   > **Note:** The `docker-compose.yml` in Step 3 is for infrastructure services only (Postgres, Dozzle) — neither needs Traefik labels. The FastAPI app's routing to `search.ndequity.org` is configured in Dokploy's service settings when deploying the app (set the domain there; Dokploy injects the Traefik labels automatically).

6. **Install Ollama directly on the VPS** (not in Docker — it's a persistent system service):
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ollama pull nomic-embed-text
   ```

7. **Verify Ollama is running:**
   ```bash
   curl http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "search_document: test"}'
   ```
   Confirm the response contains an `embedding` array of 768 floats.

### Verification

- [ ] SSH access to VPS works
- [ ] Dokploy dashboard is accessible at `dokploy-search.ndequity.org` with valid Cloudflare cert
- [ ] `search.ndequity.org` and `dokploy-search.ndequity.org` A records resolve to VPS IP
- [ ] `curl -I https://dokploy-search.ndequity.org` shows Cloudflare cert (not Traefik self-signed)
- [ ] `search.ndequity.org` will return a Cloudflare 525 SSL handshake error at this stage — that's expected. Traefik has no router for it yet since nothing is deployed. It will resolve in Step 3 when the app is deployed and Dokploy configures the router.
- [ ] Dokploy is watching the `production` branch
- [ ] `ollama list` on VPS shows `nomic-embed-text`
- [ ] Ollama embedding endpoint returns a 768-dim vector

Note: SSH tunnel verification happens in Step 2 via `make tunnel`, which forwards ports 5432 (Postgres), 11434 (Ollama), and 9999 (Dozzle).

---

## Step 2: Project Skeleton, Dependencies, and Makefile

**What:** Create the monorepo directory structure, Python venv and dependencies, `.env` configuration, Makefile with `start-dev`/`stop-dev` commands, and the database connection module.

**Why:** Establishes conventions (file layout, dependency management, env var patterns, dev workflow) that all subsequent steps follow. The Makefile gives a single command to spin up the full dev environment.

### Files to create

**`api/requirements.txt`**
```
# Core
fastapi==0.115.*
uvicorn[standard]==0.34.*
psycopg2-binary==2.9.*
pgvector==0.3.*
httpx==0.28.*

# Embeddings and ML (Step 5 will add sentence-transformers, rank-bm25)
# Listed here as comments for visibility; install when needed

# PII (Step 6 will add presidio-analyzer, spacy)

# Observability (Step 4 will add langsmith)

# Anthropic (Step 4)
# anthropic==0.*
```

Only install what this step actually needs. The comments document what's coming so the file isn't surprising later.

**`api/requirements-dev.txt`**
```
-r requirements.txt
pytest==8.*
pytest-asyncio==0.25.*
```

**`.env.example`**
```
# VPS connection (SSH tunnel makes these appear as localhost)
VPS_HOST=<your-vps-ip>
VPS_USER=root

# Postgres
POSTGRES_USER=nec_rag
POSTGRES_PASSWORD=localdev
POSTGRES_DB=nec_rag
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# Ollama
OLLAMA_BASE_URL=http://localhost:11434

# Embedding model
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSION=768
```

**`.env`** — copy of `.env.example` with your real VPS IP and password. Already in `.gitignore`.

Both local development (via SSH tunnel) and the deployed app use `localhost` for POSTGRES_HOST and OLLAMA_BASE_URL — the tunnel makes VPS ports appear local. The `VPS_HOST` and `VPS_USER` variables are only used by the Makefile to establish the tunnel.

**`api/db/__init__.py`** — empty

**`api/db/connection.py`**
```python
import os

import psycopg2


def get_connection():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
```

This follows the NEC platform tasks service pattern: a simple function returning a psycopg2 connection, configured via environment variables.

**`api/ingestion/__init__.py`** — empty (skeleton for Step 2)
**`api/orchestration/__init__.py`** — empty (skeleton for Step 4)
**`api/tasks/__init__.py`** — empty (skeleton for Step 4)
**`api/tests/__init__.py`** — empty

**`Makefile`**
```makefile
include .env
export

PIDS_DIR := .pids
LOGS_DIR := .logs

.PHONY: start-dev stop-dev logs tunnel api frontend status

# ── Dev lifecycle ──────────────────────────────────────────────

start-dev: tunnel api
	@echo "Dev environment running. Use 'make logs' to tail output, 'make stop-dev' to shut down."

stop-dev:
	@echo "Stopping dev environment..."
	@if [ -f $(PIDS_DIR)/api.pid ]; then \
		kill $$(cat $(PIDS_DIR)/api.pid) 2>/dev/null; \
		rm $(PIDS_DIR)/api.pid; \
		echo "  Stopped API"; \
	fi
	@if [ -f $(PIDS_DIR)/frontend.pid ]; then \
		kill $$(cat $(PIDS_DIR)/frontend.pid) 2>/dev/null; \
		rm $(PIDS_DIR)/frontend.pid; \
		echo "  Stopped frontend"; \
	fi
	@if [ -f $(PIDS_DIR)/tunnel.pid ]; then \
		kill $$(cat $(PIDS_DIR)/tunnel.pid) 2>/dev/null; \
		rm $(PIDS_DIR)/tunnel.pid; \
		echo "  Stopped SSH tunnel"; \
	fi
	@rm -f $(LOGS_DIR)/*.log
	@echo "Done. Logs cleared."

# ── Individual services ───────────────────────────────────────

tunnel:
	@mkdir -p $(PIDS_DIR) $(LOGS_DIR)
	@if [ -f $(PIDS_DIR)/tunnel.pid ] && kill -0 $$(cat $(PIDS_DIR)/tunnel.pid) 2>/dev/null; then \
		echo "SSH tunnel already running (PID $$(cat $(PIDS_DIR)/tunnel.pid))"; \
	else \
		ssh -f -N \
			-L 5432:localhost:5432 \
			-L 11434:localhost:11434 \
			-L 9999:localhost:9999 \
			$(VPS_USER)@$(VPS_HOST) & \
		echo $$! > $(PIDS_DIR)/tunnel.pid; \
		sleep 1; \
		if kill -0 $$(cat $(PIDS_DIR)/tunnel.pid) 2>/dev/null; then \
			echo "SSH tunnel started (PID $$(cat $(PIDS_DIR)/tunnel.pid))"; \
		else \
			echo "SSH tunnel failed to start"; \
			rm $(PIDS_DIR)/tunnel.pid; \
			exit 1; \
		fi \
	fi

api:
	@mkdir -p $(PIDS_DIR) $(LOGS_DIR)
	@if [ -f $(PIDS_DIR)/api.pid ] && kill -0 $$(cat $(PIDS_DIR)/api.pid) 2>/dev/null; then \
		echo "API already running (PID $$(cat $(PIDS_DIR)/api.pid))"; \
	else \
		cd api && .venv/bin/uvicorn orchestration.app:app --reload --port 8000 \
			> ../$(LOGS_DIR)/api.log 2>&1 & \
		echo $$! > $(PIDS_DIR)/api.pid; \
		echo "API started (PID $$(cat $(PIDS_DIR)/api.pid)) — logs at $(LOGS_DIR)/api.log"; \
	fi

frontend:
	@mkdir -p $(PIDS_DIR) $(LOGS_DIR)
	@if [ ! -f frontend/package.json ]; then \
		echo "frontend/package.json not found — skipping (created in Step 9)"; \
	elif [ -f $(PIDS_DIR)/frontend.pid ] && kill -0 $$(cat $(PIDS_DIR)/frontend.pid) 2>/dev/null; then \
		echo "Frontend already running (PID $$(cat $(PIDS_DIR)/frontend.pid))"; \
	else \
		cd frontend && npm run dev \
			> ../$(LOGS_DIR)/frontend.log 2>&1 & \
		echo $$! > $(PIDS_DIR)/frontend.pid; \
		echo "Frontend started (PID $$(cat $(PIDS_DIR)/frontend.pid)) — logs at $(LOGS_DIR)/frontend.log"; \
	fi

# ── Utilities ─────────────────────────────────────────────────

logs:
	@if command -v lnav >/dev/null 2>&1; then \
		lnav $(LOGS_DIR)/; \
	else \
		echo "Install lnav for a better experience: brew install lnav"; \
		tail -f $(LOGS_DIR)/*.log; \
	fi

status:
	@echo "=== Dev environment status ==="
	@if [ -f $(PIDS_DIR)/tunnel.pid ] && kill -0 $$(cat $(PIDS_DIR)/tunnel.pid) 2>/dev/null; then \
		echo "  SSH tunnel:  running (PID $$(cat $(PIDS_DIR)/tunnel.pid))"; \
	else \
		echo "  SSH tunnel:  stopped"; \
	fi
	@if [ -f $(PIDS_DIR)/api.pid ] && kill -0 $$(cat $(PIDS_DIR)/api.pid) 2>/dev/null; then \
		echo "  API:         running (PID $$(cat $(PIDS_DIR)/api.pid))"; \
	else \
		echo "  API:         stopped"; \
	fi
	@if [ -f $(PIDS_DIR)/frontend.pid ] && kill -0 $$(cat $(PIDS_DIR)/frontend.pid) 2>/dev/null; then \
		echo "  Frontend:    running (PID $$(cat $(PIDS_DIR)/frontend.pid))"; \
	else \
		echo "  Frontend:    stopped"; \
	fi

# ── Setup ─────────────────────────────────────────────────────

setup-api:
	cd api && python -m venv .venv
	cd api && .venv/bin/pip install -r requirements-dev.txt

verify:
	cd api && .venv/bin/python -m scripts.verify_setup

test:
	cd api && .venv/bin/pytest tests/ -v
```

Key Makefile design decisions:
- **`start-dev` runs `tunnel` + `api`** — the frontend target is separate since it won't exist until Step 9. Once Step 9 is done, change `start-dev` to `start-dev: tunnel api frontend`.
- **`ssh -f -N`** — `-f` backgrounds after authentication, `-N` means no remote command (tunnel only). The `&` and PID capture happen because `-f` forks and the parent exits, so we need the background trick to get the PID.
- **Graceful re-entry** — each target checks if the process is already running before starting a new one. `make start-dev` is safe to call repeatedly.
- **`make status`** — quick check of what's running.
- **`make setup-api`** — one-time venv creation and dependency install.
- **`make verify`** and **`make test`** — run verification and tests using the venv's Python.
- **`make logs`** — opens log files in `lnav` if installed (`brew install lnav`), falls back to `tail -f`. lnav provides syntax highlighting, filtering, and multi-file navigation.

### .gitignore additions

Add to the existing `.gitignore`:
```
# Dev environment
.pids/
.logs/
.venv/
```

### Verification

- [ ] `make setup-api` creates the venv and installs dependencies
- [ ] `cd api && .venv/bin/python -c "from db.connection import get_connection"` imports without error
- [ ] `make start-dev` starts the tunnel and API (API will fail until Step 4 creates `orchestration/app.py` — that's expected)
- [ ] `make status` shows tunnel as running
- [ ] `make stop-dev` cleans everything up
- [ ] `make status` shows all stopped

---

## Step 3: Docker Compose for VPS Services

**What:** Create a `docker-compose.yml` that runs Postgres + pgvector on the VPS. This is the single database instance used by both local development (via SSH tunnel) and the deployed production app.

**Why:** Postgres needs to be always-running on the VPS. Docker Compose makes it reproducible and manageable. Ollama is already installed as a system service in Step 1 — it doesn't need to be in the Compose file.

### Files to create

**`docker-compose.yml`**
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
      - ./api/db/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql
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

Key design decisions:
- **`127.0.0.1:5432:5432`** — binds Postgres to localhost only. Not exposed to the public internet. Local development reaches it via SSH tunnel.
- **`127.0.0.1:9999:8080`** — Dozzle bound to localhost only. Access via SSH tunnel at `http://localhost:9999`. Shows combined logs from all Docker containers on the VPS (Postgres now, deployed FastAPI later). ~15MB image, negligible CPU/RAM.
- **`restart: unless-stopped`** — both services come back up after VPS reboots.
- **`./api/db/schema.sql`** — the schema file lives in the `api/` subtree but is mounted from the repo root where `docker-compose.yml` lives.
- **No Ollama in Compose** — Ollama is installed as a system service (Step 1) since it's a persistent, shared service. Keeping it outside Docker avoids a container-in-container layer and simplifies model management.
- **No Traefik labels here** — `db` and `dozzle` are internal services, not publicly routed. The FastAPI app's domain (`search.ndequity.org`) is configured in Dokploy's service UI when the app is deployed; Dokploy injects Traefik labels automatically. See `docs/traefik-cloudflare-cert-setup.md` for the label pattern if manual configuration is needed.
- **`POSTGRES_PASSWORD` has no default** — forces setting a real password via `.env` on the VPS. The `.env.example` provides `localdev` as guidance but the Compose file won't start without an explicit value.
- The schema SQL file is mounted into Postgres's `docker-entrypoint-initdb.d/` so tables are created automatically on first `docker compose up`.

### Deploy to VPS

Clone the repo on the VPS (or copy the Compose file and schema), create a `.env` with a real password, and start:

```bash
ssh root@<vps-ip>
cd /opt/nec-rag  # or wherever you prefer
git clone <repo-url> .
cp .env.example .env
# Edit .env: set a real POSTGRES_PASSWORD and VPS_HOST
docker compose up -d
```

### Verification

Run these on the VPS:

- [ ] `docker compose up -d` starts Postgres without errors
- [ ] `docker compose exec db psql -U nec_rag -c "SELECT 1"` returns 1
- [ ] `docker compose exec db psql -U nec_rag -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"` shows the pgvector version

Then from your local Mac (with `make tunnel` or `make start-dev`):

- [ ] `psql -h localhost -U nec_rag -c "SELECT 1"` connects through the tunnel
- [ ] `http://localhost:9999` opens Dozzle and shows the `db` container logs

---

## Step 4: Database Schema

**What:** Write the SQL schema for the `practices` and `resource_chunks` tables with pgvector columns, HNSW indexes, and metadata columns.

**Why:** The schema is the foundation for all ingestion (Steps 2-3) and retrieval (Step 5). Getting the column types, constraints, and indexes right now avoids migrations later.

### Files to create

**`api/db/schema.sql`**
```sql
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

### Schema design notes

- **`npi_number` as unique key on practices** — ingestion upserts on this. Matches the spec: "idempotent — re-running on already-ingested data produces no duplicate records."
- **`content_hash` as unique key on resource_chunks** — ingestion upserts on content hash. Matches the spec: "upsert on content hash."
- **`services`, `specialties`, `presence_types` as `TEXT[]`** — matches the NEC platform's `ChoiceArrayField` pattern where these are stored as slug arrays. GIN indexes support `@>` (contains) and `&&` (overlap) operators for filtering.
- **`professionals` as JSONB** — array of `{name, title, credentials}` objects. Stored as structured data for display but not individually searchable.
- **`embedding_model VARCHAR(100)`** — stores the model version string (e.g., `nomic-embed-text:v1.5`). Required for re-embed safety per the spec.
- **`section_header` on resource_chunks** — the spec says chunks get "the section heading prepended." Storing it separately lets retrieval use it for display without parsing.
- **HNSW with `vector_cosine_ops`** — cosine distance is the standard distance metric for nomic-embed-text.
- **No HNSW tuning parameters specified** — pgvector's defaults (`m=16`, `ef_construction=64`) are reasonable for the prototype's data volume (hundreds to low thousands of records). Tuning comes in Step 5 if retrieval latency targets aren't met.

### Verification

Run these on the VPS (reset the database to pick up the schema):

- [ ] `docker compose down -v && docker compose up -d` recreates the database with the schema
- [ ] `docker compose exec db psql -U nec_rag -c "\dt"` shows both tables
- [ ] `docker compose exec db psql -U nec_rag -c "\di"` shows all indexes including HNSW indexes
- [ ] `docker compose exec db psql -U nec_rag -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'practices' ORDER BY ordinal_position;"` shows expected columns

---

## Step 5: End-to-End Verification Script

**What:** Write a Python script that generates an embedding via Ollama, inserts it into the practices table, queries it back via pgvector similarity search, and confirms the round trip works.

**Why:** This is the acceptance test for the entire step. Run locally with the SSH tunnel open (`make tunnel`) — it confirms your Mac can reach Ollama and Postgres on the VPS, and that the embedding-to-storage pipeline works end-to-end. If this script passes, Steps 2-3 (ingestion) have a proven foundation to build on.

### Files to create

**`api/scripts/__init__.py`** — empty

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

### Test

**`api/tests/test_verify_setup.py`**
```python
"""
Integration test for the embedding-to-storage pipeline.
Requires running Postgres + pgvector (via SSH tunnel or localhost).
"""

import os

import pytest
from pgvector.psycopg2 import register_vector

from db.connection import get_connection

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_HOST"),
    reason="Requires running Postgres (set POSTGRES_HOST)",
)


@pytest.fixture
def db_conn():
    conn = get_connection()
    register_vector(conn)
    yield conn
    conn.rollback()
    conn.close()


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

### Verification

- [ ] `make tunnel` establishes the SSH tunnel
- [ ] `make verify` completes with "All checks passed"
- [ ] `make test` — all tests pass

---

## Files Summary

| File | Status | Description |
|------|--------|-------------|
| `Makefile` | New | Dev lifecycle commands: start-dev, stop-dev, logs, status, setup-api, verify, test |
| `.env.example` | New | Template for environment variables (VPS connection, Postgres, Ollama) |
| `.env` | New | Local env vars with real VPS IP (gitignored) |
| `docker-compose.yml` | New | VPS services: Postgres + pgvector (Ollama installed separately as system service) |
| `api/requirements.txt` | New | Python dependencies (FastAPI, psycopg2, pgvector, httpx, uvicorn) |
| `api/requirements-dev.txt` | New | Dev dependencies (includes requirements.txt + pytest) |
| `api/db/__init__.py` | New | Empty package init |
| `api/db/schema.sql` | New | DDL for practices and resource_chunks tables with pgvector indexes |
| `api/db/connection.py` | New | `get_connection()` function returning psycopg2 connection |
| `api/ingestion/__init__.py` | New | Empty package init (skeleton for Step 2) |
| `api/orchestration/__init__.py` | New | Empty package init (skeleton for Step 4) |
| `api/tasks/__init__.py` | New | Empty package init (skeleton for Step 4) |
| `api/scripts/__init__.py` | New | Empty package init |
| `api/scripts/verify_setup.py` | New | End-to-end verification: Ollama → pgvector round trip |
| `api/tests/__init__.py` | New | Empty package init |
| `api/tests/test_verify_setup.py` | New | Integration tests for schema and vector operations |

---

## Completion Checklist

- [ ] Step 1: Provision Hetzner VPS and configure Dokploy
- [ ] Step 2: Project skeleton, dependencies, and Makefile
- [ ] Step 3: Docker Compose for VPS services
- [ ] Step 4: Database schema
- [ ] Step 5: End-to-end verification script
