#!/usr/bin/env python3
"""Apply (or re-apply) the ADE trading-mirror migration to a Postgres DB.

Reads SUPABASE_DB_URL from a gitignored .env (ade/supabase-sync/.env or repo
root) or the environment, then executes schema/0001_trading_mirror.sql. The
migration is idempotent, so this is safe to run repeatedly.

Usage:
  .venv/bin/python ade/supabase-sync/apply_migration.py
  .venv/bin/python ade/supabase-sync/apply_migration.py --check   # verify only

Exit codes: 0 ok, 1 error, 2 configuration error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

import sync_theses  # same dir; reuse .env loader + config

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
EXPECTED_TABLES = ["accounts", "holdings", "journal", "sync_runs", "theses"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify the schema/tables exist; do not apply.",
    )
    args = parser.parse_args(argv)

    sync_theses._load_dotenv(sync_theses.REPO_ROOT)
    dsn, schema = sync_theses._require_config()

    try:
        if args.check:
            with psycopg.connect(dsn) as conn:
                got = conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                    (schema,),
                ).fetchall()
            names = sorted(r[0] for r in got)
            missing = [t for t in EXPECTED_TABLES if t not in names]
            if not missing:
                print(f"OK — {schema}.{{{', '.join(EXPECTED_TABLES)}}} present")
                return 0
            print(
                f"NOT READY — {schema} missing {missing} "
                f"(has {names or 'nothing'}). Run without --check to apply."
            )
            return 1

        migrations = sorted(SCHEMA_DIR.glob("*.sql"))
        if not migrations:
            sys.stderr.write(f"ERROR: no migrations in {SCHEMA_DIR}\n")
            return 1
        # No params -> psycopg uses the simple-query protocol, so each
        # multi-statement migration runs in one call. All are idempotent.
        with psycopg.connect(dsn, autocommit=True) as conn:
            for mig in migrations:
                conn.execute(mig.read_text())
                print(f"Applied {mig.name}")
        print(f"All migrations applied -> schema '{schema}'")
        return 0
    except psycopg.OperationalError as exc:
        sys.stderr.write(
            f"ERROR: cannot connect (check SUPABASE_DB_URL): {exc}\n"
        )
        return 1
    except psycopg.Error as exc:
        sys.stderr.write(f"ERROR: migration failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
