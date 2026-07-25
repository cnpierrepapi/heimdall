-- Record whether a leaderboard row can be scored honestly at all.
--
-- Adds two columns to hd_agents. Additive and nullable-safe: existing rows take
-- the default and the console ignores columns it does not read, so this can be
-- applied before the console change that uses it.
--
-- score_state separates the two nulls that were previously reported the same
-- way. An 'insufficient' row needs more settled evidence and will eventually get
-- a verdict. An 'unscoreable' row never will, because this deployment has no
-- source of truth for that kind of work: ownership, domain and glossary terms
-- are confirmed by an organization, not derivable from anything an agent reads.
-- Reporting both as "not enough data yet" promises a score that is not coming.
--
-- score_reason carries the explanation the console shows next to the row.

alter table hd_agents
  add column if not exists score_state text not null default 'insufficient',
  add column if not exists score_reason text;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'hd_agents_score_state_check'
  ) then
    alter table hd_agents
      add constraint hd_agents_score_state_check
      check (score_state in ('scored', 'insufficient', 'unscoreable'));
  end if;
end $$;
