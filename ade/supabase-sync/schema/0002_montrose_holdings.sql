-- ADE Montrose holdings mirror. Additive, isolated in the `trading` schema.
-- Idempotent: safe to re-run. Apply after 0001_trading_mirror.sql.
--
-- Montrose is a Swedish broker (multi-account, multi-currency: SEK + USD +
-- others). Holdings are stored as point-in-time daily snapshots so history
-- is preserved; the natural key (account, as_of, instrument_key) makes
-- re-syncing the same day idempotent.

create schema if not exists trading;

-- ---------------------------------------------------------------------------
-- accounts: one row per Montrose account (ISK / KF / Depot / ...).
-- ---------------------------------------------------------------------------
create table if not exists trading.accounts (
    account_id     uuid primary key,
    account_number text,
    account_name   text,
    account_type   text,
    raw            jsonb not null,
    synced_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- holdings: daily point-in-time snapshot per account/instrument.
-- `doc` is the durable raw holding; promoted columns are for querying.
-- instrument_key = orderbook_id, else ticker, else instrument_name
-- (computed by the sync helper) so the unique key is always populated.
-- ---------------------------------------------------------------------------
create table if not exists trading.holdings (
    id              bigint generated always as identity primary key,
    account_id      uuid not null references trading.accounts (account_id)
                        on delete cascade,
    as_of           date not null,
    instrument_key  text not null,
    orderbook_id    text,
    ticker          text,
    instrument_name text,
    quantity        numeric,
    avg_price       numeric,
    market_value    numeric,
    currency        text,
    doc             jsonb not null,
    content_hash    text not null,
    synced_at       timestamptz not null default now(),
    unique (account_id, as_of, instrument_key)
);

create index if not exists idx_holdings_account on trading.holdings (account_id);
create index if not exists idx_holdings_as_of   on trading.holdings (as_of);
create index if not exists idx_holdings_ticker  on trading.holdings (ticker);
create index if not exists idx_holdings_currency on trading.holdings (currency);
create index if not exists idx_holdings_doc_gin  on trading.holdings using gin (doc);

-- Convenience: most recent snapshot per account.
create or replace view trading.holdings_latest as
select h.*
from trading.holdings h
join (
    select account_id, max(as_of) as as_of
    from trading.holdings
    group by account_id
) m on m.account_id = h.account_id and m.as_of = h.as_of;

-- ---------------------------------------------------------------------------
-- Lock down (same posture as 0001): RLS on, no policies; service_role only.
-- ---------------------------------------------------------------------------
alter table trading.accounts enable row level security;
alter table trading.holdings enable row level security;

grant usage on schema trading to service_role;
grant all on all tables in schema trading to service_role;
grant all on all sequences in schema trading to service_role;
alter default privileges in schema trading
    grant all on tables to service_role;
alter default privileges in schema trading
    grant all on sequences to service_role;
