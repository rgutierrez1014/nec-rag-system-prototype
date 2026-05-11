# nec-rag-system-prototype

An AI-powered natural language search for the Neurodivergent Equity Coalition platform. A FastAPI RAG service prototype backed by Postgres + pgvector and Ollama embeddings.

This prototype is scoped to San Francisco County, containing NPI data for the county as well as curated resources from local ND and disability organizations and other relevant resources from a national/non-scoped level.

## Architecture

Instead of a more traditional Docker Compose setup, this project utilizes a combination of locally venv-ed backend and frontend, plus Ollama and postgres and dozzle persisting on the VPS and tunneled for local use. This was done to save on memory; I'm on a 10 year old Macbook and doubt my computer's capability to run normal apps plus the models needed for this.

```
Ollama + nomic-embed-text   > VPS (tunnel)
Postgres db                 > VPS (tunnel)
Dozzle (combined logs)      > VPS (tunnel)
FastAPI                     > Local (venv)
NextJS                      > Local (venv)
```

The Postgres db gets a separate database per use

```
nec_rag             > Prod DB
nec_rag_dev         > Dev DB
nec_rag_test        > Test DB (spun up and torn down every test run)
```

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
