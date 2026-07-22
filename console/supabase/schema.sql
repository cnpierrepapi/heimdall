-- Heimdall console schema: the public projection the console reads.
--
-- Three read-only tables, one per console panel:
--   hd_activity  the live feed of observed gateway calls
--   hd_findings  catalog-grounded issues, each citing the catalog fact it broke
--   hd_agents    the leaderboard, one row per (agent, work_kind)
--
-- These are a projection of the SQLite ledger and event store, produced by
-- scripts/publish_snapshot.py and loaded by an operator holding the service
-- role key. Nothing writes here except the service role: anon and authenticated
-- may only SELECT, guarded by RLS. The console reads with the anon key.
--
-- owner is the tenant. The public showcase uses owner = 'showcase'; real
-- per-user data is scoped by owner under RLS once auth lands (see F1). catalog
-- names the DataHub catalog the rows came from (the demo world is 'lineworld').

-- ---------------------------------------------------------------------------
-- activity: every agent tool call observed at the gateway
-- ---------------------------------------------------------------------------
create table if not exists hd_activity (
  id             bigint generated always as identity primary key,
  ts             timestamptz not null default now(),
  agent_id       text not null,
  tool           text not null,
  op             text not null default 'read',      -- read | write
  args           jsonb,
  entities       jsonb,                              -- catalog urns touched
  latency_ms     integer,
  status         text not null default 'ok',         -- ok | error | blocked | held
  result_summary text,
  owner          text not null default 'showcase',
  catalog        text not null default 'lineworld'
);

create index if not exists idx_hd_activity_owner_ts
  on hd_activity (owner, ts desc);

-- ---------------------------------------------------------------------------
-- findings: catalog-grounded problems with observed actions
-- ---------------------------------------------------------------------------
create table if not exists hd_findings (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null default now(),
  agent_id    text not null,
  activity_id bigint references hd_activity (id) on delete cascade,
  check_type  text not null,        -- undefined_column | glossary_conflict | ...
  severity    text not null default 'warn',   -- info | warn | harmful
  verdict     text,
  entity_urn  text,
  "column"    text,                 -- column is a reserved word; keep it quoted
  reason      text,
  owner       text not null default 'showcase',
  catalog     text not null default 'lineworld'
);

create index if not exists idx_hd_findings_owner_ts
  on hd_findings (owner, ts desc);

-- ---------------------------------------------------------------------------
-- agents: the leaderboard, one row per (agent, work_kind)
-- ---------------------------------------------------------------------------
create table if not exists hd_agents (
  agent_id   text not null,
  work_kind  text not null,        -- column_doc | table_doc | pii | owner | ...
  trust      numeric,
  verdict    text,
  n_settled  integer default 0,
  brier      numeric,
  win_rate   numeric,
  visibility text not null default 'public'
             check (visibility in ('public', 'private')),
  owner      text,
  catalog    text,
  updated_at timestamptz not null default now(),
  primary key (agent_id, work_kind)
);

create index if not exists idx_hd_agents_trust on hd_agents (trust desc);

-- ---------------------------------------------------------------------------
-- Tenancy: map auth users to tenants (owners).
-- ---------------------------------------------------------------------------
create table if not exists hd_members (
  user_id    uuid not null references auth.users (id) on delete cascade,
  owner      text not null,
  role       text not null default 'member',
  created_at timestamptz not null default now(),
  primary key (user_id, owner)
);

-- Resolve the caller's tenants without exposing hd_members to anon. Lives in an
-- unexposed schema so it is not a public /rpc endpoint, and runs as definer so
-- RLS policies can call it. Returns nothing for anonymous callers.
create schema if not exists heimdall;
grant usage on schema heimdall to anon, authenticated;

create or replace function heimdall.current_owners()
returns setof text
language sql
stable
security definer
set search_path = public
as $$
  select owner from public.hd_members where user_id = auth.uid()
$$;

revoke all on function heimdall.current_owners() from public;
grant execute on function heimdall.current_owners() to anon, authenticated;

-- ---------------------------------------------------------------------------
-- Row level security. The public showcase (owner = 'showcase') stays
-- anon-readable; authenticated users also see rows for tenants they belong to.
-- Public agents are visible to everyone; a private agent's scores are readable
-- only by its owning tenant. Only the service role writes (the publisher).
-- ---------------------------------------------------------------------------
alter table hd_activity enable row level security;
alter table hd_findings enable row level security;
alter table hd_agents   enable row level security;
alter table hd_members  enable row level security;

drop policy if exists hd_members_self_read on hd_members;
create policy hd_members_self_read on hd_members
  for select to authenticated using (user_id = auth.uid());

drop policy if exists hd_activity_read on hd_activity;
create policy hd_activity_read on hd_activity for select using (
  owner = 'showcase' or owner in (select heimdall.current_owners())
);

drop policy if exists hd_findings_read on hd_findings;
create policy hd_findings_read on hd_findings for select using (
  owner = 'showcase' or owner in (select heimdall.current_owners())
);

drop policy if exists hd_agents_read on hd_agents;
create policy hd_agents_read on hd_agents for select using (
  visibility = 'public'
  or owner = 'showcase'
  or owner in (select heimdall.current_owners())
);

revoke all on hd_activity, hd_findings, hd_agents, hd_members from anon, authenticated;
grant select on hd_activity, hd_findings, hd_agents to anon, authenticated;
grant select on hd_members to authenticated;
grant all on hd_activity, hd_findings, hd_agents, hd_members to service_role;
