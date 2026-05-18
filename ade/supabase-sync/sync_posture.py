#!/usr/bin/env python3
"""ADE Supabase sync for the market posture + forces digest.

Reads the latest exposure-coach posture JSON (reports/exposure_posture_*.json)
and an optional forces-digest text file, and upserts one row per day into
trading.market_posture via the scoped Keychain credential — same direct-PG
path as the other sidecars (never imports/edits upstream code).

Idempotent: keyed by as_of (one posture row per day).

Exit codes: 0 ok, 1 sync error, 2 configuration error.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import sync_theses as st  # reuse .env/Keychain resolver + REPO_ROOT

_COLS = [
    "as_of",
    "generated_at",
    "exposure_ceiling",
    "recommendation",
    "bias",
    "participation",
    "confidence",
    "component_scores",
    "forces_digest",
    "doc",
    "content_hash",
]


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return None


def _as_of(doc: dict[str, Any], path: Path) -> str:
    val = _first(doc, "as_of", "date", "generated_at")
    if isinstance(val, str):
        m = re.search(r"\d{4}-\d{2}-\d{2}", val)
        if m:
            return m.group(0)
    m = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
    return m.group(0) if m else date.today().isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--posture",
        default=None,
        help="Posture JSON (default: newest reports/exposure_posture_*.json)",
    )
    parser.add_argument(
        "--forces",
        default=None,
        help="Optional forces-digest text/markdown file to attach",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.posture:
        p = Path(args.posture)
    else:
        found = sorted(
            glob.glob(str(st.REPO_ROOT / "reports" / "exposure_posture_*.json"))
        )
        if not found:
            sys.stderr.write(
                "ERROR: no reports/exposure_posture_*.json (run exposure-coach"
                " first)\n"
            )
            return 2
        p = Path(found[-1])
    if not p.is_file():
        sys.stderr.write(f"ERROR: posture file not found: {p}\n")
        return 2

    doc = json.loads(p.read_text())
    forces = None
    if args.forces:
        fp = Path(args.forces)
        if fp.is_file():
            forces = fp.read_text()
        else:
            sys.stderr.write(f"WARN: forces file not found: {fp}\n")

    as_of = _as_of(doc, p)
    row = {
        "as_of": as_of,
        "generated_at": _first(doc, "generated_at", "timestamp") or as_of,
        "exposure_ceiling": _first(
            doc, "exposure_ceiling", "exposure_ceiling_pct", "ceiling"
        ),
        "recommendation": _first(doc, "recommendation", "action"),
        "bias": _first(doc, "bias", "bias_direction"),
        "participation": _first(doc, "participation", "participation_status"),
        "confidence": _first(doc, "confidence", "confidence_level"),
        "component_scores": Jsonb(
            _first(doc, "component_scores", "scores", "dimensions") or {}
        ),
        "forces_digest": forces,
        "doc": Jsonb({"posture": doc, "_source_file": p.name}),
    }
    canon = json.dumps(
        {k: (v.obj if isinstance(v, Jsonb) else v) for k, v in row.items()},
        sort_keys=True,
        default=str,
    )
    row["content_hash"] = hashlib.sha256(canon.encode()).hexdigest()

    def log(m: str) -> None:
        if not args.quiet:
            print(m)

    if args.dry_run:
        log(f"[dry-run] posture {p.name} as_of={as_of}")
        log(
            f"[dry-run] ceiling={row['exposure_ceiling']} "
            f"rec={row['recommendation']} conf={row['confidence']} "
            f"forces={'yes' if forces else 'no'}"
        )
        return 0

    st._load_dotenv(st.REPO_ROOT)
    dsn, schema = st._require_config()
    cols = ", ".join(_COLS)
    ph = ", ".join(f"%({c})s" for c in _COLS)
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS if c != "as_of")
    sql = (
        f"INSERT INTO {schema}.market_posture ({cols}, synced_at) "
        f"VALUES ({ph}, now()) "
        f"ON CONFLICT (as_of) DO UPDATE SET {upd}, synced_at = now()"
    )
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, row)
            conn.commit()
    except psycopg.OperationalError as exc:
        sys.stderr.write(
            f"ERROR: cannot connect (check Keychain credential): {exc}\n"
        )
        return 1
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        sys.stderr.write(
            "ERROR: trading.market_posture missing — apply migration 0004 "
            "(admin-gated): ade/supabase-sync/apply_migration.py\n"
        )
        return 1
    except psycopg.Error as exc:
        sys.stderr.write(f"ERROR: posture sync failed: {exc}\n")
        return 1

    log(f"Synced market posture as_of {as_of} -> {schema}.market_posture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
