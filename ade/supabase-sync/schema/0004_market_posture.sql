-- ADE market-posture mirror. Additive, idempotent. Apply after 0001-0003
-- (needs the `ade_sync` role from 0003). Daily posture snapshots keyed by
-- as_of so re-running the same day is idempotent.
--
-- Self-contained: re-declares the ade_sync grant + RLS policy for this table
-- (0003's policy loop only covered the original five tables).

create schema if not exists trading;

create table if not exists trading.market_posture (
    as_of            date primary key,
    generated_at     timestamptz not null default now(),
    exposure_ceiling numeric,
    recommendation   text,
    bias             text,
    participation    text,
    confidence       text,
    component_scores jsonb,
    forces_digest    text,
    doc              jsonb not null,
    content_hash     text not null,
    synced_at        timestamptz not null default now()
);

create index if not exists idx_posture_generated
    on trading.market_posture (generated_at);

-- Lock down (same posture as the other tables): RLS on, no anon/auth policy.
alter table trading.market_posture enable row level security;

-- Privileges for the least-privilege sync role (no DELETE, no DDL).
grant usage on schema trading to ade_sync;
grant select, insert, update on trading.market_posture to ade_sync;

-- ade_sync is nobypassrls -> needs an explicit policy or RLS blocks it too.
drop policy if exists ade_sync_rw on trading.market_posture;
create policy ade_sync_rw on trading.market_posture
    for all to ade_sync using (true) with check (true);
