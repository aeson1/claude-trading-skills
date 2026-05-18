-- ADE least-privilege sync role. Additive, idempotent. Apply after 0001+0002.
--
-- Security model: the recurring sidecar must NOT use service_role or the
-- postgres superuser. `ade_sync` can only SELECT/INSERT/UPDATE inside the
-- `trading` schema — no DELETE, no DDL, nothing in `public`, no superuser,
-- no RLS bypass. Worst case if its credential leaks: someone can read/write
-- a mirror of data that already lives in local files.
--
-- The password is NOT set here (never hardcode a secret in a tracked file).
-- bootstrap_scoped_role.py sets it via ALTER ROLE and stores the resulting
-- connection string in the macOS Keychain.

-- Role: no login until bootstrap grants it; minimal attributes.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'ade_sync') then
    create role ade_sync nologin nosuperuser nocreatedb nocreaterole
      noinherit nobypassrls;
  end if;
end
$$;

-- Privileges: trading schema only. Explicitly NO delete, NO public, NO ddl.
grant usage on schema trading to ade_sync;
grant select, insert, update on all tables in schema trading to ade_sync;
grant usage, select on all sequences in schema trading to ade_sync;
alter default privileges in schema trading
  grant select, insert, update on tables to ade_sync;
alter default privileges in schema trading
  grant usage, select on sequences to ade_sync;

-- RLS stays ON with no policies for anon/authenticated (they get nothing).
-- ade_sync is non-superuser / nobypassrls, so it needs an explicit policy
-- on each table or RLS will block it too. FOR ALL but DML is already capped
-- to select/insert/update by the GRANTs above (no delete grant => no delete).
do $$
declare t text;
begin
  foreach t in array array[
    'theses','journal','sync_runs','accounts','holdings'
  ]
  loop
    execute format(
      'drop policy if exists ade_sync_rw on trading.%I', t);
    execute format(
      'create policy ade_sync_rw on trading.%I '
      'for all to ade_sync using (true) with check (true)', t);
  end loop;
end
$$;
