include .env
export

PIDS_DIR := .pids
LOGS_DIR := .logs

# Shell snippet used by tunnel-kill, tunnel-restart, and stop-dev.
# Kills tunnel via pid file first, then falls back to pgrep for strays.
define TUNNEL_KILL_CMDS
	@if [ -f $(PIDS_DIR)/tunnel.pid ]; then \
		pid=$$(cat $(PIDS_DIR)/tunnel.pid); \
		kill $$pid 2>/dev/null && echo "  Killed SSH tunnel (PID $$pid from .pids file)"; \
		rm -f $(PIDS_DIR)/tunnel.pid; \
	fi
	@pids=$$(pgrep -f "ssh.*-N.*$(VPS_HOST)" 2>/dev/null || true); \
	if [ -n "$$pids" ]; then \
		echo $$pids | xargs kill 2>/dev/null || true; \
		echo "  Killed stray SSH tunnel processes: $$pids"; \
		rm -f $(PIDS_DIR)/tunnel.pid; \
	fi
endef

define API_KILL_CMDS
	@if [ -f $(PIDS_DIR)/api.pid ]; then \
		pid=$$(cat $(PIDS_DIR)/api.pid); \
		kill $$pid 2>/dev/null && echo "  Killed API (PID $$pid from .pids file)"; \
		rm -f $(PIDS_DIR)/api.pid; \
	fi
	@pids=$$(pgrep -f "uvicorn orchestration.app:app" 2>/dev/null || true); \
	if [ -n "$$pids" ]; then \
		echo $$pids | xargs kill 2>/dev/null || true; \
		echo "  Killed stray API processes: $$pids"; \
		rm -f $(PIDS_DIR)/api.pid; \
	fi
endef

.PHONY: start-dev stop-dev logs tunnel tunnel-kill tunnel-restart api frontend status setup-api setup-db apply-migrations verify test ingest-npi

# ── Dev lifecycle ──────────────────────────────────────────────

start-dev: tunnel-restart api
	@echo "Dev environment running. Use 'make logs' to tail output, 'make stop-dev' to shut down."

stop-dev:
	@echo "Stopping dev environment..."
	$(API_KILL_CMDS)
	@if [ -f $(PIDS_DIR)/frontend.pid ]; then \
		kill $$(cat $(PIDS_DIR)/frontend.pid) 2>/dev/null; \
		rm $(PIDS_DIR)/frontend.pid; \
		echo "  Stopped frontend"; \
	fi
	$(TUNNEL_KILL_CMDS)
	@rm -f $(LOGS_DIR)/*.log
	@echo "Done. Logs cleared."

# ── Individual services ───────────────────────────────────────

tunnel:
	@mkdir -p $(PIDS_DIR) $(LOGS_DIR)
	@if [ -f $(PIDS_DIR)/tunnel.pid ] && kill -0 $$(cat $(PIDS_DIR)/tunnel.pid) 2>/dev/null; then \
		echo "SSH tunnel already running (PID $$(cat $(PIDS_DIR)/tunnel.pid))"; \
	else \
		ssh -N \
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

tunnel-kill:
	$(TUNNEL_KILL_CMDS)

tunnel-restart: tunnel-kill
	@$(MAKE) tunnel

api:
	@mkdir -p $(PIDS_DIR) $(LOGS_DIR)
	@if [ -f $(PIDS_DIR)/api.pid ] && kill -0 $$(cat $(PIDS_DIR)/api.pid) 2>/dev/null; then \
		echo "API already running (PID $$(cat $(PIDS_DIR)/api.pid))"; \
	elif pgrep -f "uvicorn orchestration.app:app" >/dev/null 2>&1; then \
		echo "API already running (PID $$(pgrep -f 'uvicorn orchestration.app:app'))"; \
	else \
		cd api && .venv/bin/uvicorn orchestration.app:app --reload --port 8000 \
			> ../$(LOGS_DIR)/api.log 2>&1 & \
		sleep 1; \
		pid=$$(pgrep -f "uvicorn orchestration.app:app" | head -1); \
		echo $$pid > $(PIDS_DIR)/api.pid; \
		echo "API started (PID $$pid) — logs at $(LOGS_DIR)/api.log"; \
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
		pids=$$(pgrep -f "ssh.*-N.*$(VPS_HOST)" 2>/dev/null || true); \
		if [ -n "$$pids" ]; then \
			echo "  SSH tunnel:  running (PID $$pids) [stale .pids file — run 'make tunnel-restart' to resync]"; \
		else \
			echo "  SSH tunnel:  stopped"; \
		fi \
	fi
	@if [ -f $(PIDS_DIR)/api.pid ] && kill -0 $$(cat $(PIDS_DIR)/api.pid) 2>/dev/null; then \
		echo "  API:         running (PID $$(cat $(PIDS_DIR)/api.pid))"; \
	else \
		pids=$$(pgrep -f "uvicorn orchestration.app:app" 2>/dev/null || true); \
		if [ -n "$$pids" ]; then \
			echo "  API:         running (PID $$pids) [stale .pids file — run 'make stop-dev && make start-dev' to resync]"; \
		else \
			echo "  API:         stopped"; \
		fi \
	fi
	@if [ -f $(PIDS_DIR)/frontend.pid ] && kill -0 $$(cat $(PIDS_DIR)/frontend.pid) 2>/dev/null; then \
		echo "  Frontend:    running (PID $$(cat $(PIDS_DIR)/frontend.pid))"; \
	else \
		echo "  Frontend:    stopped"; \
	fi

# ── Setup ─────────────────────────────────────────────────────

setup-api:
	cd api && python3 -m venv .venv
	cd api && .venv/bin/pip install -r requirements-dev.txt

db ?= nec_rag_dev

setup-db:
	@echo "Requires SSH tunnel (make tunnel). Creating database and applying migrations to $(db)..."
	cd api && .venv/bin/python -m scripts.setup_db $(db)

apply-migrations:
	cd api && .venv/bin/python -m scripts.apply_migrations $(db)

verify:
	cd api && .venv/bin/python -m scripts.verify_setup

test:
	@echo "Requires SSH tunnel (make tunnel)."
	cd api && .venv/bin/pytest tests/ -v

ingest-npi:
	@echo "Requires SSH tunnel (make tunnel)."
	cd api && .venv/bin/python -m ingestion.ingest_npi --data-path ../data/npi_full.csv
