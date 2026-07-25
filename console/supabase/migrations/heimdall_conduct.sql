-- Record what each agent did, alongside how well it did it.
--
-- Trust needs something to settle the claim. Conduct does not: how many writes
-- an agent attempted, how many the gateway blocked or held, and how many drew a
-- grounded finding are all true the moment the action is observed.
--
-- This is what the console shows in place of a rank for agents whose work
-- cannot be scored in this deployment. Withholding a score is not the same as
-- having nothing to report, and an agent inventing teams that do not exist is
-- worth watching precisely because no number will ever say so.
--
-- Additive with zero defaults, so existing rows stay valid and the publisher can
-- start filling these before the console reads them.

alter table hd_agents
  add column if not exists n_actions  integer default 0,
  add column if not exists n_applied  integer default 0,
  add column if not exists n_blocked  integer default 0,
  add column if not exists n_held     integer default 0,
  add column if not exists n_harmful  integer default 0,
  add column if not exists n_warn     integer default 0,
  add column if not exists n_entities integer default 0,
  add column if not exists clean_rate numeric;
