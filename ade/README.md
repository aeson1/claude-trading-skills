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

## exposure-adapter — fix the sibling-skill schema gap

`ade/exposure-adapter/adapt_inputs.py` normalizes `market-breadth-analyzer`
and `market-top-detector` JSON (which nest the score under
`composite.composite_score`) into the top-level `breadth_score` /
`top_risk_score` keys that `exposure-coach` actually reads. Without it
exposure-coach silently drops both inputs and stays LOW-confidence /
uptrend-only. With it, exposure-coach ingests all four critical signals
(breadth/uptrend/regime/top-risk) → MEDIUM confidence. Additive; no upstream
edits (survives `git pull upstream`). top-risk uses a zone→safety map
(green 85 / yellow 65 / orange 40 / red 20) since the two composites are not
the same calibrated scale.

Flow: run the regime trio → `adapt_inputs.py` → exposure-coach with
`--breadth reports/_adapted_breadth.json --top-risk reports/_adapted_top_risk.json`.

## market-posture mirror

`schema/0004_market_posture.sql` adds `trading.market_posture` (one row per
day, keyed by `as_of`; RLS + explicit `ade_sync` policy, since 0003's policy
loop only covered the original five tables). `sync_posture.py` upserts the
latest exposure-coach posture + an optional forces-digest file via the scoped
Keychain credential.

> **Admin-gated:** 0004 is DDL and `ade_sync` cannot CREATE TABLE (by
> design). To activate: temporarily re-add `SUPABASE_ADMIN_DB_URL` (Session
> pooler) to `.env`, run `apply_migration.py` (applies 0001–0004
> idempotently), remove it again. Thereafter `sync_posture.py` runs on the
> scoped Keychain credential like the other sidecars.

## portfolio-analysis — the weekly decision document

`ade/portfolio-analysis/` (skill symlinked into `.claude/skills/`) is the
top-level deliverable: a true portfolio analysis report that ends in an
explicit prioritized **Next Steps / Decisions** block.

| Piece | What |
|---|---|
| `SKILL.md` | Claude-driven orchestration (Montrose refresh → market inputs → forces → build → PDF → persist) |
| `run_market_inputs.sh` | Driver: regime trio + ADE adapter + exposure-coach (FMP from Keychain) |
| `build_report.py` | Deterministic assembler → `reports/portfolio_analysis_<as_of>.md` |

Pipeline: holdings snapshot + exposure-coach posture (via the ADE adapter) +
macro-forces digest → markdown → **PDF** (`make-pdf` skill, cover + TOC) →
**Supabase** (`sync_posture.py` → `trading.market_posture`, admin-gated 0004).

**Cadence:** weekly + event-driven (re-run on a posture regime-flip or major
forces-KB update). The report header states the next scheduled review date.
The sector map in `build_report.py` is an editable heuristic — refine as the
book changes. Immaterial trims (< max(2000 SEK, 1% of invested)) are demoted
to "monitor only" so the action list stays signal.
