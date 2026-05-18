#!/usr/bin/env python3
"""ADE Supabase sync for Montrose holdings (direct Postgres).

The Montrose tools are Claude.ai *connector* tools — they can only be called
by Claude in-conversation, not from a headless cron job. So the flow is:

  1. (Claude, via the `montrose-portfolio` skill) call GetUserAccounts +
     GetHoldings and write a snapshot JSON to state/montrose/.
  2. (this script) upsert that snapshot into trading.accounts +
     trading.holdings over SUPABASE_DB_URL — same proven psycopg path as
     sync_theses.py.

Snapshot JSON contract (what the skill writes):
  {
    "as_of": "YYYY-MM-DD",
    "accounts": [ <GetUserAccounts objects> ],
    "holdings": [ { "accountId": "...", ...raw GetHoldings fields } ]
  }

Field names inside each holding vary; this script keeps the full raw object
in `doc` and best-effort-extracts promoted columns, so it is robust to
Montrose payload changes. Multi-currency (SEK/USD/...) is preserved as-is.

Exit codes: 0 ok, 1 sync error, 2 configuration error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import sync_theses  # same dir; reuse .env loader, config, REPO_ROOT

_ACCOUNT_COLS = [
    "account_id",
    "account_number",
    "account_name",
    "account_type",
    "raw",
]
_HOLDING_COLS = [
    "account_id",
    "as_of",
    "instrument_key",
    "orderbook_id",
    "ticker",
    "instrument_name",
    "quantity",
    "avg_price",
    "market_value",
    "currency",
    "doc",
    "content_hash",
]


def _first(d: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-null value among candidate keys."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _account_rows(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for a in accounts:
        rows.append(
            {
                "account_id": _first(a, "accountId", "account_id", "id"),
                "account_number": _first(
                    a, "accountNumber", "account_number"
                ),
                "account_name": _first(a, "accountName", "account_name"),
                "account_type": _first(a, "accountType", "account_type"),
                "raw": Jsonb(a),
            }
        )
    return [r for r in rows if r["account_id"]]


def _holding_rows(
    holdings: list[dict[str, Any]], as_of: str
) -> list[dict[str, Any]]:
    rows = []
    for h in holdings:
        account_id = _first(h, "accountId", "account_id")
        orderbook_id = _first(h, "orderbookId", "orderbook_id")
        ticker = _first(h, "ticker", "symbol")
        name = _first(h, "instrumentName", "name", "instrument_name")
        instrument_key = str(
            orderbook_id
            if orderbook_id is not None
            else (ticker or name or "UNKNOWN")
        )
        canonical = json.dumps(
            h, sort_keys=True, default=str, ensure_ascii=False
        )
        if not account_id:
            sys.stderr.write(
                f"WARN: skipping holding without accountId: "
                f"{instrument_key}\n"
            )
            continue
        rows.append(
            {
                "account_id": account_id,
                "as_of": as_of,
                "instrument_key": instrument_key,
                "orderbook_id": (
                    str(orderbook_id) if orderbook_id is not None else None
                ),
                "ticker": ticker,
                "instrument_name": name,
                "quantity": _first(h, "quantity", "volume", "qty"),
                "avg_price": _first(
                    h, "averageAcquiredPrice", "avgPrice", "average_price"
                ),
                "market_value": _first(
                    h, "marketValue", "value", "market_value"
                ),
                "currency": _first(h, "currency", "currencyCode"),
                "doc": Jsonb(h),
                "content_hash": hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest(),
            }
        )
    return rows


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
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO {schema}.{table} ({col_list}, synced_at) "
        f"VALUES ({placeholders}, now()) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}, "
        f"synced_at = now()"
    )
    cur.executemany(sql, rows)


def main(argv: list[str] | None = None) -> int:
    default_dir = sync_theses.REPO_ROOT / "state" / "montrose"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default=None,
        help="Snapshot JSON (default: newest *.json in state/montrose/)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.input:
        snap_path = Path(args.input)
    else:
        candidates = sorted(default_dir.glob("*.json")) if (
            default_dir.is_dir()
        ) else []
        if not candidates:
            sys.stderr.write(
                f"ERROR: no snapshot JSON found in {default_dir} "
                f"(run the montrose-portfolio skill first)\n"
            )
            return 2
        snap_path = candidates[-1]

    if not snap_path.is_file():
        sys.stderr.write(f"ERROR: snapshot not found: {snap_path}\n")
        return 2

    snap = json.loads(snap_path.read_text())
    as_of = snap.get("as_of") or date.today().isoformat()
    accounts = _account_rows(snap.get("accounts") or [])
    holdings = _holding_rows(snap.get("holdings") or [], as_of)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg)

    if args.dry_run:
        log(f"[dry-run] snapshot   : {snap_path.name} (as_of {as_of})")
        log(f"[dry-run] accounts   : {len(accounts)}")
        log(f"[dry-run] holdings   : {len(holdings)}")
        ccy = sorted({h["currency"] for h in holdings if h["currency"]})
        log(f"[dry-run] currencies : {', '.join(ccy) or 'n/a'}")
        return 0

    sync_theses._load_dotenv(sync_theses.REPO_ROOT)
    dsn, schema = sync_theses._require_config()
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                _upsert(
                    cur,
                    schema,
                    "accounts",
                    _ACCOUNT_COLS,
                    "account_id",
                    accounts,
                )
                _upsert(
                    cur,
                    schema,
                    "holdings",
                    _HOLDING_COLS,
                    "account_id, as_of, instrument_key",
                    holdings,
                )
            conn.commit()
    except psycopg.OperationalError as exc:
        sys.stderr.write(
            f"ERROR: cannot connect (check SUPABASE_DB_URL): {exc}\n"
        )
        return 1
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        sys.stderr.write(
            "ERROR: trading.accounts/holdings missing — apply migrations: "
            "ade/supabase-sync/apply_migration.py\n"
        )
        return 1
    except psycopg.Error as exc:
        sys.stderr.write(f"ERROR: holdings sync failed: {exc}\n")
        return 1

    log(
        f"Synced {len(accounts)} accounts, {len(holdings)} holdings "
        f"(as_of {as_of}) -> {schema} schema"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
