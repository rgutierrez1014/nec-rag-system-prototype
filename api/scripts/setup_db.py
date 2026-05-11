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
    target = sys.argv[1] if len(sys.argv) > 1 else "nec_rag_dev"
    print(f"Setting up database: {target}")
    create_database_if_not_exists(target)
    print("\nApplying migrations...")
    apply_migrations(target)
    print("\nDone.")


if __name__ == "__main__":
    main()
