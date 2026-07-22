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
-- Row level security: anon and authenticated read; only the service role
-- writes (the publisher). Revoke the write and DDL-adjacent grants Postgres
-- hands out by default so a fresh provision matches the intended posture.
-- ---------------------------------------------------------------------------
alter table hd_activity enable row level security;
alter table hd_findings enable row level security;
alter table hd_agents   enable row level security;

drop policy if exists hd_activity_read on hd_activity;
create policy hd_activity_read on hd_activity for select using (true);

drop policy if exists hd_findings_read on hd_findings;
create policy hd_findings_read on hd_findings for select using (true);

drop policy if exists hd_agents_read on hd_agents;
create policy hd_agents_read on hd_agents for select using (true);

revoke all on hd_activity, hd_findings, hd_agents from anon, authenticated;
grant select on hd_activity, hd_findings, hd_agents to anon, authenticated;
grant all on hd_activity, hd_findings, hd_agents to service_role;
