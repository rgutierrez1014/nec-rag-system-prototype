include .env
export

PIDS_DIR := .pids
LOGS_DIR := .logs

.PHONY: start-dev stop-dev logs tunnel api frontend status setup-api setup-db apply-migrations verify test

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
	cd api && python3 -m venv .venv
	cd api && .venv/bin/pip install -r requirements-dev.txt

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

verify:
	cd api && .venv/bin/python -m scripts.verify_setup

test:
	@echo "Requires SSH tunnel (make tunnel)."
	cd api && .venv/bin/pytest tests/ -v
