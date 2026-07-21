-- heimdall scoreboard schema: three read-only projections of the ledger.
-- Writes happen only through the publisher (service role); anon may SELECT.

create table if not exists ll_agents (
  agent_id text primary key,
  model_id text,
  trust numeric not null,
  verdict text not null,
  n_total integer not null default 0,
  n_settled integer not null default 0,
  wins integer,
  win_rate numeric,
  brier_mean numeric,
  ece numeric,
  p_value numeric,
  q_value numeric,
  expected_null_wins numeric,
  updated_at timestamptz not null default now()
);

create table if not exists ll_calibration (
  agent_id text not null references ll_agents (agent_id) on delete cascade,
  bin_low numeric not null,
  bin_high numeric not null,
  n integer not null,
  mean_confidence numeric not null,
  frac_true numeric not null,
  primary key (agent_id, bin_low)
);

create table if not exists ll_claims (
  claim_id text primary key,
  agent_id text not null,
  model_id text,
  claim_type text not null,
  entity_urn text not null,
  prediction jsonb not null default '{}'::jsonb,
  confidence numeric not null,
  created_ts double precision not null,
  settled_ts double precision,
  outcome jsonb,
  correct boolean
);

create index if not exists idx_ll_claims_agent on ll_claims (agent_id, created_ts desc);

alter table ll_agents enable row level security;
alter table ll_calibration enable row level security;
alter table ll_claims enable row level security;

drop policy if exists ll_agents_read on ll_agents;
create policy ll_agents_read on ll_agents for select to anon, authenticated using (true);

drop policy if exists ll_calibration_read on ll_calibration;
create policy ll_calibration_read on ll_calibration for select to anon, authenticated using (true);

drop policy if exists ll_claims_read on ll_claims;
create policy ll_claims_read on ll_claims for select to anon, authenticated using (true);
