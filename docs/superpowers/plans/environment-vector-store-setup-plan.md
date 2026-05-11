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

- [x] SSH access to VPS works
- [x] Dokploy dashboard is accessible at `dokploy-search.ndequity.org` with valid Cloudflare cert
- [x] `search.ndequity.org` and `dokploy-search.ndequity.org` A records resolve to VPS IP
- [x] `curl -I https://dokploy-search.ndequity.org` shows Cloudflare cert (not Traefik self-signed)
- [x] `search.ndequity.org` will return a Cloudflare 525 SSL handshake error at this stage — that's expected. Traefik has no router for it yet since nothing is deployed. It will resolve in Step 3 when the app is deployed and Dokploy configures the router.
- [x] Dokploy is watching the `production` branch
- [x] `ollama list` on VPS shows `nomic-embed-text`
- [x] Ollama embedding endpoint returns a 768-dim vector

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

**Deviations:** `setup-api` uses `python3` instead of `python` — no pyenv or version manager installed; Homebrew Python 3.11.4 is at `/usr/local/bin/python3`. Tunnel/status/stop-dev verifications deferred to Step 3/4 when VPS and app are in place.
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

- [x] `make setup-api` creates the venv and installs dependencies
- [x] `cd api && .venv/bin/python -c "from db.connection import get_connection"` imports without error
- [x] `make start-dev` starts the tunnel and API (API will fail until Step 4 creates `orchestration/app.py` — that's expected)
- [x] `make status` shows tunnel as running
- [x] `make stop-dev` cleans everything up
- [x] `make status` shows all stopped

---

## Step 3: Three-Database Isolation (Docker Compose, Schema, Migrations, Verification)

**What:** Deploy docker-compose, set up three isolated databases via yoyo-migrations, create the verification script and test suite. This step supersedes the original Steps 3–5 by integrating database isolation from the start.

**Why:** Rather than creating a single shared database and retrofitting isolation later, we do it right the first time: `nec_rag` (production), `nec_rag_dev` (local development), `nec_rag_test` (test suite). Schema is managed as numbered SQL migration files via yoyo-migrations.

**Implementation:** See [`2026-05-10-three-database-isolation-plan.md`](2026-05-10-three-database-isolation-plan.md) for the full 5-step implementation plan with file contents, verification checklists, and test code.

### Summary of what gets created

| File | Description |
|------|-------------|
| `api/db/migrations/0001_initial.sql` | Initial schema (practices + resource_chunks tables, HNSW indexes) |
| `api/scripts/setup_db.py` | Creates databases and applies yoyo migrations |
| `api/scripts/__init__.py` | Empty package init |
| `api/scripts/verify_setup.py` | End-to-end Ollama → pgvector round-trip verification |
| `api/tests/conftest.py` | Session fixture for test DB lifecycle + `db_conn` fixture |
| `api/tests/test_verify_setup.py` | Integration tests for schema and vector operations |

| File | Change |
|------|--------|
| `api/requirements.txt` | Add `yoyo-migrations==8.*` |
| `.env.example` | `POSTGRES_DB=nec_rag_dev` (was `nec_rag`) |
| `Makefile` | Add `setup-db`, `apply-migrations` targets; update `test` with tunnel reminder |
| `docker-compose.yml` | Remove `schema.sql` init mount |

### Key design decisions

- **No `api/db/schema.sql`** — replaced by `api/db/migrations/0001_initial.sql` with identical SQL content. yoyo tracks which migrations have been applied per-database.
- **`make setup-db`** — Python script that connects to the `postgres` default DB, creates `nec_rag` and `nec_rag_dev`, then applies all migrations to both.
- **Test lifecycle (Django-style)** — `conftest.py` creates `nec_rag_test` at session start, applies migrations, yields, then drops it. Per-test rollback isolation.
- **No `skipif` guard** — if the tunnel isn't open, conftest fails fast with a connection error. Same signal, less boilerplate.
- **Production migrations** — applied via `entrypoint.sh` on container start (deferred to deployment step). yoyo is idempotent.

### Verification

- [x] `docker compose up -d` on VPS starts Postgres
- [x] `make setup-db` creates both databases and applies migrations
- [x] `make verify` completes with "All checks passed"
- [x] `make test` passes all tests (creates/drops `nec_rag_test` automatically)
- [x] `http://localhost:9999` opens Dozzle and shows container logs

---

## Files Summary

| File | Status | Description |
|------|--------|-------------|
| `Makefile` | New | Dev lifecycle commands: start-dev, stop-dev, logs, status, setup-api, setup-db, apply-migrations, verify, test |
| `.env.example` | New | Template for environment variables (VPS connection, Postgres, Ollama) |
| `.env` | New | Local env vars with real VPS IP (gitignored) |
| `docker-compose.yml` | New | VPS services: Postgres + pgvector (no schema init mount — migrations handle schema) |
| `api/requirements.txt` | New | Python dependencies (FastAPI, psycopg2, pgvector, httpx, uvicorn, yoyo-migrations) |
| `api/requirements-dev.txt` | New | Dev dependencies (includes requirements.txt + pytest) |
| `api/db/__init__.py` | New | Empty package init |
| `api/db/migrations/0001_initial.sql` | New | Initial schema (practices + resource_chunks tables, HNSW indexes) |
| `api/db/connection.py` | New | `get_connection()` function returning psycopg2 connection |
| `api/ingestion/__init__.py` | New | Empty package init (skeleton for Step 2) |
| `api/orchestration/__init__.py` | New | Empty package init (skeleton for Step 4) |
| `api/tasks/__init__.py` | New | Empty package init (skeleton for Step 4) |
| `api/scripts/__init__.py` | New | Empty package init |
| `api/scripts/setup_db.py` | New | Creates databases and applies yoyo migrations |
| `api/scripts/verify_setup.py` | New | End-to-end verification: Ollama → pgvector round trip |
| `api/tests/__init__.py` | New | Empty package init |
| `api/tests/conftest.py` | New | Session fixture for test DB lifecycle + `db_conn` fixture |
| `api/tests/test_verify_setup.py` | New | Integration tests for schema and vector operations |

---

## Completion Checklist

- [x] Step 1: Provision Hetzner VPS and configure Dokploy
- [x] Step 2: Project skeleton, dependencies, and Makefile
- [x] Step 3: Three-database isolation (see [implementation plan](2026-05-10-three-database-isolation-plan.md))
