# nec-rag-system-prototype

AI-powered natural language search for the Neurodivergent Equity Coalition platform. A FastAPI RAG service scoped to San Francisco County, backed by Postgres + pgvector and Ollama embeddings. The API is the product — the Next.js frontend (coming in a later step) is a demo client.

## Prerequisites

- Python 3.11+
- SSH access to the Hetzner VPS (hosts Postgres and Ollama permanently)
- A `.env` file at the project root (copy `.env.example` and fill in `VPS_HOST` and `POSTGRES_PASSWORD`)

## First-time setup

```bash
# 1. Create the Python venv and install dependencies
make setup-api

# 2. Open the SSH tunnel to the VPS
make tunnel

# 3. Create databases and apply migrations
make setup-db
```

`make setup-db` defaults to `nec_rag_dev`. Pass `db=<name>` to target a specific database:

```bash
make setup-db              # → nec_rag_dev
make setup-db db=nec_rag   # → nec_rag (production)
```

Run both to initialize all databases on first-time VPS setup.

## Daily development

```bash
make start-dev   # opens SSH tunnel + starts FastAPI on :8000
make stop-dev    # shuts everything down and clears logs
make status      # check what's running
make logs        # tail logs (lnav if installed, otherwise tail -f)
```

The SSH tunnel makes VPS services appear local:
- `localhost:5432` → Postgres
- `localhost:11434` → Ollama
- `localhost:9999` → Dozzle (container log viewer)

## Verification and tests

```bash
make verify   # end-to-end check: Ollama embedding → pgvector round-trip
make test     # integration test suite (creates and drops nec_rag_test automatically)
```

Both require the SSH tunnel to be open (`make tunnel`).

## Schema changes

Add a new numbered migration file under `api/db/migrations/` (e.g., `0002_add_search_logs.sql`) and run:

```bash
make apply-migrations              # → nec_rag_dev (default)
make apply-migrations db=nec_rag   # → nec_rag (production)
make setup-db db=nec_rag           # create + migrate nec_rag if it doesn't exist yet
```

## Deployment

Push to the `production` branch. Dokploy on the VPS watches that branch, builds the FastAPI container, and deploys it. The container connects to the same Postgres and Ollama as local dev — via `localhost` instead of the SSH tunnel.
