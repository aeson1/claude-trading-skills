-- ADE Supabase mirror for trader-memory-core state.
--
-- This is ADDITIVE and isolated in its own `trading` schema so it never
-- collides with other apps on the same Postgres (e.g. public.profiles,
-- public.forces, ...). Safe to run against a hosted Supabase project.
--
-- Apply on your HOSTED project (one of):
--   * Supabase dashboard -> SQL Editor -> paste this file -> Run
--   * supabase db execute --file ade/supabase-sync/schema/0001_trading_mirror.sql
--   * psql "$SUPABASE_DB_URL" -f ade/supabase-sync/schema/0001_trading_mirror.sql
--
-- AFTER applying, expose the schema to PostgREST so the sidecar can reach it:
--   Dashboard -> Project Settings -> API -> "Exposed schemas" -> add: trading
--
-- Idempotent: safe to re-run.

create schema if not exists trading;

-- ---------------------------------------------------------------------------
-- theses: full thesis document as jsonb + promoted scalar columns.
-- The jsonb `doc` is the durable mirror; promoted columns exist only for
-- indexing/filtering. New upstream fields land in `doc` automatically and
-- never break this mirror.
-- ---------------------------------------------------------------------------
create table if not exists trading.theses (
    thesis_id        text primary key,
    ticker           text not null,
    thesis_type      text,
    setup_type       text,
    status           text,
    confidence_score numeric,
    created_at       timestamptz,
    updated_at       timestamptz,
    next_review_date date,
    review_status    text,
    pnl_dollars      numeric,
    pnl_pct          numeric,
    holding_days     integer,
    source_file      text,
    content_hash     text not null,
    doc              jsonb not null,
    synced_at        timestamptz not null default now()
);

create index if not exists idx_theses_ticker      on trading.theses (ticker);
create index if not exists idx_theses_status      on trading.theses (status);
create index if not exists idx_theses_type        on trading.theses (thesis_type);
create index if not exists idx_theses_review_date on trading.theses (next_review_date);
create index if not exists idx_theses_doc_gin     on trading.theses using gin (doc);

-- ---------------------------------------------------------------------------
-- journal: postmortem markdown (state/journal/pm_<thesis_id>.md).
-- source_file is the natural key so re-sync is idempotent.
-- ---------------------------------------------------------------------------
create table if not exists trading.journal (
    source_file  text primary key,
    thesis_id    text references trading.theses (thesis_id) on delete set null,
    kind         text not null default 'postmortem',
    entry_date   date,
    body         text not null,
    content_hash text not null,
    synced_at    timestamptz not null default now()
);

create index if not exists idx_journal_thesis on trading.journal (thesis_id);

-- ---------------------------------------------------------------------------
-- sync_runs: lightweight observability for the sidecar.
-- ---------------------------------------------------------------------------
create table if not exists trading.sync_runs (
    id               bigint generated always as identity primary key,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    theses_upserted  integer not null default 0,
    journal_upserted integer not null default 0,
    status           text not null default 'running',
    error            text
);

-- ---------------------------------------------------------------------------
-- Lock down: RLS enabled, NO policies. anon/authenticated get nothing.
-- service_role has BYPASSRLS in Supabase, so the sidecar (service key) works
-- while the data stays invisible to public/auth API callers.
-- ---------------------------------------------------------------------------
alter table trading.theses    enable row level security;
alter table trading.journal   enable row level security;
alter table trading.sync_runs enable row level security;

-- Privileges: a fresh custom schema has none for the API roles. Grant only
-- service_role; deliberately do NOT grant anon/authenticated.
grant usage on schema trading to service_role;
grant all on all tables in schema trading to service_role;
grant all on all sequences in schema trading to service_role;
alter default privileges in schema trading
    grant all on tables to service_role;
alter default privileges in schema trading
    grant all on sequences to service_role;
