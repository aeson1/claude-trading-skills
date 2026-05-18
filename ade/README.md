# `ade/` — ADE Advisory customizations

Everything in this directory is **ours**, layered on top of the upstream
`tradermonty/claude-trading-skills` project. It is intentionally **outside**
`skills/` so it does not trigger upstream's docs-completeness / catalog /
distribution machinery, and so `git pull upstream main` never conflicts here.

Branch model:
- `main` tracks upstream (`git pull upstream main` stays clean).
- `ade-customizations` carries our work (this dir, `.gitignore` hardening).
- `origin` = our fork (`github.com/aeson1/claude-trading-skills`, backup).

## supabase-sync — durable mirror of trade state

Upstream gitignores `state/`, so `state/theses/*.yaml` and
`state/journal/pm_*.md` exist **only on this machine**. `supabase-sync` mirrors
them into a hosted Supabase project so the data survives a lost folder and is
queryable in SQL. It is a **sidecar**: it never imports or edits upstream
`trader-memory-core` code, so upstream updates can't break it.

### Data model (`trading` schema, isolated from any other app on the DB)

| Table               | Key                | Contents                                            |
|---------------------|--------------------|-----------------------------------------------------|
| `trading.theses`    | `thesis_id`        | full thesis as `jsonb doc` + promoted query columns |
| `trading.journal`   | `source_file`      | postmortem markdown (`pm_<id>.md`)                   |
| `trading.sync_runs` | `id`               | per-run observability                               |

The `jsonb doc` is the durable copy; promoted columns are only for
indexing/filtering. New upstream thesis fields land in `doc` automatically and
never break the mirror. The sidecar talks **direct Postgres** (psycopg), not
PostgREST — so there is **no "expose schema" dashboard step**.

### Credential model (least privilege)

The recurring sync must never hold a god credential. Posture:

- **RLS on, no policies** for `anon`/`authenticated` → public API sees
  nothing.
- A dedicated **`ade_sync`** Postgres role: `SELECT/INSERT/UPDATE` on the
  `trading` schema only — **no DELETE, no DDL, no `public`, not superuser,
  no RLS bypass** (explicit per-table policy). Worst case if leaked:
  read/write a mirror of data that already lives in local files.
- That credential lives in the **macOS Keychain** (service
  `ade-supabase-sync`, account `ade_sync`), not in any file.
- The admin DB password / `service_role` key are used **once** at bootstrap
  and then removed from `.env`. Sidecars never use them.

Credential precedence in the sidecars: `$SUPABASE_DB_URL` (escape hatch) →
Keychain → `.env`.

### One-time setup (hosted project)

1. **Python deps** (gitignored venv):
   `.venv/bin/pip install -r ade/supabase-sync/requirements.txt`
2. **Admin URL for bootstrap only**:
   `cp ade/supabase-sync/.env.example ade/supabase-sync/.env`, set
   `SUPABASE_ADMIN_DB_URL` to the **Session pooler** URI.
3. **Bootstrap** (applies migrations, creates+scopes `ade_sync`, stores it
   in Keychain, self-verifies the privilege scoping):
   `.venv/bin/python ade/supabase-sync/bootstrap_scoped_role.py`
4. **Delete `SUPABASE_ADMIN_DB_URL`** from `.env` (and the rotated
   service-role key — it is not used). `apply_migration.py --check`
   verifies the schema later without admin.

Optional hardening: Dashboard → Database → Network Restrictions → allowlist
your IP(s), so even a leaked scoped credential is unusable elsewhere.

### Run

```bash
# parse + preview, no DB connection needed
.venv/bin/python ade/supabase-sync/sync_theses.py --dry-run

# real sync (scoped Keychain credential, transactional + idempotent)
.venv/bin/python ade/supabase-sync/sync_theses.py
```

Upserts are idempotent (primary-key merge) — safe to run repeatedly, e.g. from
cron or a launchd agent after the trade-memory workflows write state.

## montrose — live broker book → analysis + mirror

Montrose is a Swedish broker exposed via a **claude.ai connector** (tools are
Claude-invoked, not headless). The `montrose-portfolio` skill
(`ade/montrose-portfolio/SKILL.md`, symlinked into `.claude/skills/`) drives the live
flow; `ade/supabase-sync` does the deterministic DB write.

| Piece | What |
|---|---|
| `ade/montrose-portfolio/SKILL.md` | Claude-driven: accounts → holdings → multi-currency analysis → mirror → human-in-loop trade-assist |
| `ade/supabase-sync/schema/0002_montrose_holdings.sql` | `trading.accounts` + `trading.holdings` (daily snapshots, multi-currency) |
| `ade/supabase-sync/sync_holdings.py` | Upserts a snapshot JSON → Supabase (same psycopg path) |

Flow: the skill calls `get_user_accounts` + `get_holdings`, writes
`state/montrose/holdings_<date>.json`, then:

```bash
.venv/bin/python ade/supabase-sync/sync_holdings.py --dry-run   # preview
.venv/bin/python ade/supabase-sync/sync_holdings.py             # upsert
```

`apply_migration.py` applies **all** `schema/*.sql` in order (0001 + 0002),
idempotently. `--check` verifies the full table set.

`create_trade_ticket` only ever produces a pre-filled Montrose URL the user
confirms in-app — it never executes an order, and the skill never invokes it
without an explicit request.
