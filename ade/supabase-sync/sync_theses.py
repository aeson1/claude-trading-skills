#!/usr/bin/env python3
"""ADE Supabase sync sidecar for trader-memory-core (direct Postgres).

Mirrors local file-based thesis state into a hosted Supabase project over a
direct Postgres connection (SUPABASE_DB_URL). No PostgREST, so no "expose
schema" dashboard step is required.

This is a SIDECAR: it never imports or edits upstream trader-memory-core code.
The local `state/` files remain the source of truth; Supabase is a durable,
queryable mirror (the repo gitignores `state/`, so this is the only
off-machine backup of trade state).

Reads:
  state/theses/th_*.yaml   -> <schema>.theses   (full doc as jsonb + columns)
  state/journal/pm_*.md    -> <schema>.journal  (postmortem markdown)

Config (env vars; a gitignored .env in ade/supabase-sync/ or the repo root is
loaded if present, real environment variables win):
  SUPABASE_DB_URL      Postgres URI (Dashboard -> Settings -> Database)
  SUPABASE_DB_SCHEMA   optional, default "trading"

Upserts are transactional and idempotent (primary-key merge). Safe to run on a
cron/launchd.

Exit codes: 0 ok, 1 sync error, 2 configuration error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

REPO_ROOT = Path(__file__).resolve().parents[2]

# Real environment captured at import, BEFORE any .env is loaded, so the
# credential precedence is: real env var > macOS Keychain > .env file.
_ENV_DSN_AT_IMPORT = os.environ.get("SUPABASE_DB_URL", "").strip()

KEYCHAIN_SERVICE = "ade-supabase-sync"
KEYCHAIN_ACCOUNT = "ade_sync"

_THESIS_COLS = [
    "thesis_id",
    "ticker",
    "thesis_type",
    "setup_type",
    "status",
    "confidence_score",
    "created_at",
    "updated_at",
    "next_review_date",
    "review_status",
    "pnl_dollars",
    "pnl_pct",
    "holding_days",
    "source_file",
    "content_hash",
    "doc",
]
_JOURNAL_COLS = [
    "source_file",
    "thesis_id",
    "kind",
    "entry_date",
    "body",
    "content_hash",
]


# --- config -----------------------------------------------------------------


def _load_dotenv(repo_root: Path) -> None:
    """Load KEY=VALUE lines from a gitignored .env (no dependency).

    Checks, in order, the location next to the example and the repo root.
    Real environment variables always take precedence; first file wins.
    """
    candidates = [
        repo_root / "ade" / "supabase-sync" / ".env",
        repo_root / ".env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _keychain_get(service: str, account: str) -> str:
    """Read a generic-password item from the macOS Keychain (empty if none)."""
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _resolve_db_url() -> str:
    """Credential precedence: real env var > Keychain > .env-loaded env."""
    if _ENV_DSN_AT_IMPORT:
        return _ENV_DSN_AT_IMPORT
    kc = _keychain_get(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    if kc:
        return kc
    return os.environ.get("SUPABASE_DB_URL", "").strip()


def _require_config() -> tuple[str, str]:
    dsn = _resolve_db_url()
    schema = os.environ.get("SUPABASE_DB_SCHEMA", "trading").strip() or "trading"
    if not dsn:
        sys.stderr.write(
            "ERROR: no Supabase DB credential found.\n"
            f"  Looked in: $SUPABASE_DB_URL, Keychain "
            f"({KEYCHAIN_SERVICE}/{KEYCHAIN_ACCOUNT}), .env.\n"
            "  Run the one-time bootstrap: "
            "ade/supabase-sync/bootstrap_scoped_role.py\n"
        )
        raise SystemExit(2)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        sys.stderr.write(f"ERROR: invalid SUPABASE_DB_SCHEMA: {schema!r}\n")
        raise SystemExit(2)
    return dsn, schema


# --- thesis parsing ---------------------------------------------------------


def _content_hash(doc: dict[str, Any]) -> str:
    canonical = json.dumps(doc, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _nz(value: Any) -> Any:
    """Normalize empty strings to None so Postgres date/numeric casts work."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _thesis_row(doc: dict[str, Any], source_file: str) -> dict[str, Any]:
    monitoring = doc.get("monitoring") or {}
    outcome = doc.get("outcome") or {}
    origin = doc.get("origin") or {}
    return {
        "thesis_id": doc["thesis_id"],
        "ticker": doc["ticker"],
        "thesis_type": _nz(doc.get("thesis_type")),
        "setup_type": _nz(doc.get("setup_type")),
        "status": _nz(doc.get("status")),
        "confidence_score": _nz(doc.get("confidence_score")),
        "created_at": _nz(doc.get("created_at")),
        "updated_at": _nz(doc.get("updated_at")),
        "next_review_date": _nz(monitoring.get("next_review_date")),
        "review_status": _nz(monitoring.get("review_status")),
        "pnl_dollars": _nz(outcome.get("pnl_dollars")),
        "pnl_pct": _nz(outcome.get("pnl_pct")),
        "holding_days": _nz(outcome.get("holding_days")),
        "source_file": origin.get("output_file") or source_file,
        "content_hash": _content_hash(doc),
        "doc": Jsonb(doc),
    }


def _collect_theses(state_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(state_dir.glob("th_*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            sys.stderr.write(f"WARN: skipping unparseable {path.name}: {exc}\n")
            continue
        if not isinstance(doc, dict) or "thesis_id" not in doc:
            sys.stderr.write(f"WARN: skipping {path.name}: not a thesis doc\n")
            continue
        rows.append(_thesis_row(doc, path.name))
    return rows


def _collect_journal(journal_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not journal_dir.is_dir():
        return rows
    for path in sorted(journal_dir.glob("*.md")):
        body = path.read_text()
        stem = path.stem  # pm_<thesis_id>
        thesis_id = stem[3:] if stem.startswith("pm_") else None
        entry_date = (
            datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            .date()
            .isoformat()
        )
        rows.append(
            {
                "source_file": path.name,
                "thesis_id": thesis_id,
                "kind": "postmortem",
                "entry_date": entry_date,
                "body": body,
                "content_hash": hashlib.sha256(
                    body.encode("utf-8")
                ).hexdigest(),
            }
        )
    return rows


# --- postgres upsert --------------------------------------------------------


def _upsert(
    cur: psycopg.Cursor,
    schema: str,
    table: str,
    cols: list[str],
    conflict: str,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    col_list = ", ".join(cols)
    placeholders = ", ".join(f"%({c})s" for c in cols)
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c != conflict
    )
    sql = (
        f"INSERT INTO {schema}.{table} ({col_list}, synced_at) "
        f"VALUES ({placeholders}, now()) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}, synced_at = now()"
    )
    cur.executemany(sql, rows)


def _record_run(dsn: str, schema: str, payload: dict[str, Any]) -> None:
    """Best-effort observability write; never raises."""
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                f"INSERT INTO {schema}.sync_runs "
                "(started_at, finished_at, theses_upserted, "
                "journal_upserted, status, error) "
                "VALUES (%(started_at)s, %(finished_at)s, "
                "%(theses_upserted)s, %(journal_upserted)s, "
                "%(status)s, %(error)s)",
                payload,
            )
    except psycopg.Error:
        pass


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        default=str(REPO_ROOT / "state" / "theses"),
        help="Thesis state dir (default: <repo>/state/theses)",
    )
    parser.add_argument(
        "--journal-dir",
        default=str(REPO_ROOT / "state" / "journal"),
        help="Journal dir (default: <repo>/state/journal)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and build payloads but do not write to Supabase",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    _load_dotenv(REPO_ROOT)

    state_dir = Path(args.state_dir)
    journal_dir = Path(args.journal_dir)
    if not state_dir.is_dir():
        sys.stderr.write(f"ERROR: state dir not found: {state_dir}\n")
        return 2

    theses = _collect_theses(state_dir)
    journal = _collect_journal(journal_dir)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg)

    if args.dry_run:
        log(f"[dry-run] theses to upsert : {len(theses)}")
        log(f"[dry-run] journal to upsert: {len(journal)}")
        for r in theses[:5]:
            log(
                f"  - {r['thesis_id']} {r['ticker']} "
                f"{r['status']} hash={r['content_hash'][:8]}"
            )
        if len(theses) > 5:
            log(f"  ... +{len(theses) - 5} more")
        return 0

    dsn, schema = _require_config()
    started = datetime.now(timezone.utc).isoformat()
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                _upsert(
                    cur, schema, "theses", _THESIS_COLS, "thesis_id", theses
                )
                _upsert(
                    cur,
                    schema,
                    "journal",
                    _JOURNAL_COLS,
                    "source_file",
                    journal,
                )
            conn.commit()
    except psycopg.OperationalError as exc:
        sys.stderr.write(
            f"ERROR: cannot connect to Postgres (check SUPABASE_DB_URL): "
            f"{exc}\n"
        )
        return 1
    except psycopg.errors.InvalidSchemaName:
        sys.stderr.write(
            f"ERROR: schema '{schema}' not found — apply the migration: "
            f"ade/supabase-sync/apply_migration.py\n"
        )
        return 1
    except psycopg.errors.UndefinedTable:
        sys.stderr.write(
            f"ERROR: tables missing in '{schema}' — apply the migration: "
            f"ade/supabase-sync/apply_migration.py\n"
        )
        return 1
    except psycopg.Error as exc:
        sys.stderr.write(f"ERROR: sync failed: {exc}\n")
        _record_run(
            dsn,
            schema,
            {
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "theses_upserted": 0,
                "journal_upserted": 0,
                "status": "error",
                "error": str(exc)[:2000],
            },
        )
        return 1

    _record_run(
        dsn,
        schema,
        {
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "theses_upserted": len(theses),
            "journal_upserted": len(journal),
            "status": "ok",
            "error": None,
        },
    )
    log(
        f"Synced {len(theses)} theses, {len(journal)} journal entries "
        f"-> {schema} schema"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
