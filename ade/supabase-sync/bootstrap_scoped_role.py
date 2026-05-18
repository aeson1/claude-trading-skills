#!/usr/bin/env python3
"""One-time bootstrap: create the least-privilege ade_sync credential.

Run ONCE with an admin credential (the postgres/DB-password connection
string — the god credential used here only for bootstrap, never stored for
recurring use). This script:

  1. applies all schema/*.sql migrations (idempotent),
  2. generates a strong random password for the `ade_sync` role,
  3. sets it via ALTER ROLE,
  4. composes the `ade_sync` pooler connection string,
  5. stores that string in the macOS Keychain (service/account that the
     sidecars read), and
  6. verifies by connecting AS ade_sync — proving it can upsert the
     `trading` schema but is denied DELETE and `public`.

Secrets are never printed and never written to disk. After this runs, the
admin credential is no longer needed and should be removed from .env; the
recurring sidecars use only the scoped Keychain credential.

Admin credential source (one-time): $SUPABASE_ADMIN_DB_URL, else
$SUPABASE_DB_URL, else .env. Must be the **Session pooler** URI.

Exit codes: 0 ok, 1 error, 2 configuration error.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

import psycopg
from psycopg import sql

import sync_theses as st

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


def _fail(msg: str, code: int = 1) -> int:
    sys.stderr.write(f"ERROR: {msg}\n")
    return code


def _admin_dsn() -> str:
    st._load_dotenv(st.REPO_ROOT)
    return (
        os.environ.get("SUPABASE_ADMIN_DB_URL", "").strip()
        or os.environ.get("SUPABASE_DB_URL", "").strip()
    )


def _pooler_ref(parsed) -> str | None:
    """Project ref from a Supavisor pooler URL (user = '<role>.<ref>')."""
    user = parsed.username or ""
    if "." in user and "pooler.supabase.com" in (parsed.hostname or ""):
        return user.split(".", 1)[1]
    return None


def main() -> int:
    admin = _admin_dsn()
    if not admin:
        return _fail(
            "no admin credential. Set SUPABASE_ADMIN_DB_URL (Session pooler "
            "URI) in .env for this one-time run.",
            2,
        )

    p = urlparse(admin)
    ref = _pooler_ref(p)
    if not ref:
        return _fail(
            "admin URL must be the Session POOLER URI "
            "(user 'postgres.<ref>' @ ...pooler.supabase.com). The Direct "
            "URL is IPv6-only and its ref can't be derived reliably.",
            2,
        )

    # 1. migrations (idempotent) via admin
    try:
        migrations = sorted(SCHEMA_DIR.glob("*.sql"))
        with psycopg.connect(admin, autocommit=True) as conn:
            for mig in migrations:
                conn.execute(mig.read_text())
                print(f"applied {mig.name}")
    except psycopg.OperationalError as exc:
        return _fail(f"cannot connect with admin URL: {exc}")
    except psycopg.Error as exc:
        return _fail(f"migration failed: {exc}")

    # 2-3. generate + set ade_sync password (URL-safe; no quoting hazards)
    pw = secrets.token_urlsafe(36)
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER ROLE ade_sync WITH LOGIN PASSWORD {}").format(
                    sql.Literal(pw)
                )
            )
    except psycopg.Error as exc:
        return _fail(f"could not set ade_sync password: {exc}")

    # 4. compose ade_sync pooler connection string (same host/port/db)
    host = p.hostname or ""
    port = p.port or 5432
    dbname = (p.path or "/postgres").lstrip("/") or "postgres"
    scoped = (
        f"postgresql://ade_sync.{ref}:{quote(pw, safe='')}"
        f"@{host}:{port}/{dbname}"
    )

    # 5. store in Keychain (-U updates if present). The value is on the arg
    # list only for the brief lifetime of this local `security` process.
    try:
        res = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                st.KEYCHAIN_SERVICE,
                "-a",
                st.KEYCHAIN_ACCOUNT,
                "-w",
                scoped,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode != 0:
            return _fail(f"Keychain store failed: {res.stderr.strip()}")
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail(f"Keychain store failed: {exc}")

    # 6. verify AS ade_sync: read ok, scoped write ok (rolled back),
    #    DELETE denied, public denied.
    checks: list[str] = []
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute("SELECT 1")
            checks.append("connect=OK")
            with conn.transaction(force_rollback=True):
                conn.execute(
                    "INSERT INTO trading.theses "
                    "(thesis_id,ticker,content_hash,doc) "
                    "VALUES ('__bootstrap_probe__','PROBE','h','{}'::jsonb) "
                    "ON CONFLICT (thesis_id) DO UPDATE SET ticker='PROBE'"
                )
            checks.append("trading_upsert=OK")
            try:
                conn.execute("DELETE FROM trading.theses WHERE false")
                conn.rollback()
                checks.append("delete=UNEXPECTEDLY_ALLOWED")
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                checks.append("delete=DENIED(correct)")
            try:
                conn.execute("SELECT 1 FROM public.profiles LIMIT 1")
                checks.append("public=UNEXPECTEDLY_ALLOWED")
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                checks.append("public=DENIED(correct)")
            except psycopg.Error:
                conn.rollback()
                checks.append("public=DENIED")
    except psycopg.Error as exc:
        return _fail(
            f"ade_sync verification failed: {exc} (checks: {checks})"
        )

    bad = [c for c in checks if "UNEXPECTEDLY" in c]
    print(
        f"ade_sync stored in Keychain "
        f"({st.KEYCHAIN_SERVICE}/{st.KEYCHAIN_ACCOUNT})"
    )
    print("verification: " + " ".join(checks))
    if bad:
        return _fail(f"privilege scoping is WRONG: {bad}")
    print(
        "OK — recurring sync now uses the scoped Keychain credential. "
        "Remove SUPABASE_ADMIN_DB_URL / SUPABASE_DB_URL and the "
        "service key from .env."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
