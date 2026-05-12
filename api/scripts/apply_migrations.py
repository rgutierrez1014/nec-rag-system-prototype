"""
Apply pending yoyo migrations to a target database.

Usage:
    make apply-migrations [db=nec_rag_dev]

Requires SSH tunnel to VPS (make tunnel).
"""

import os
import sys

from yoyo import get_backend, read_migrations


MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "db", "migrations")

POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "nec_rag_dev"
    url = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{target}"

    backend = get_backend(url)
    migrations = read_migrations(MIGRATIONS_DIR)

    print(f"Target database: {target}")
    print("Running migrations:")

    with backend.lock():
        pending = list(backend.to_apply(migrations))
        if not pending:
            print("  (nothing to apply)")
            return
        for migration in pending:
            print(f"  Applying {migration.id}...", end="", flush=True)
            backend.apply_migrations([migration])
            print(" OK")


if __name__ == "__main__":
    main()
