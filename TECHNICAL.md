# Heimdall, technical notes

How the control plane is built, and the design decisions that are load bearing.
[README.md](README.md) says what it does; [SETUP.md](SETUP.md) says how to run it.

## Shape of the system

Heimdall sits in the path between an agent and DataHub. It is an MCP server that
fronts `mcp-server-datahub` and mirrors its tool surface exactly, so any MCP client
points at Heimdall instead and needs no code change.

```
  agent (any MCP client)
        |
        v
  heimdall.gateway              observe, ground, govern, annotate
        |         \
        |          +--> observability.py   every call, read and write
        |          +--> grounding.py       findings, cited against the catalog
        |          +--> policy.py          accept / pass / hold / block
        |          +--> claims.py          implicit claim per mutation
        v
  mcp-server-datahub
        |
        v
  DataHub
        ^
        |
  writeback.py + audit.py       trust tags, structured properties, dossiers
```

Everything below the gateway is a projection. `snapshot.py` builds the console rows,
`publisher.py` writes them to Supabase, and the Next.js console reads them under row
level security.

## Storage

Three SQLite databases, all in WAL mode so the gateway writes while the scoreboard
reads. SQLite is a deliberate choice: the whole scoring core runs from a clean clone
with zero services, which is what makes the project evaluable without a stack.

| Store | Module | Holds |
| --- | --- | --- |
| Claim ledger | `claims.py` | One falsifiable statement per row, with confidence, settled later |
| Event store | `observability.py` | Every observed tool call, reads included |
| Finding store | `grounding.py` | Catalog-grounded issues, each citing a fact |

An observation is a superset of a claim. A claim says "agent A asserts this
description is correct" and is settleable. An observation says "agent A called
`update_description` on this column at this time" and covers reads, which never
become claims but are most of what an agent does. They live in separate tables for
that reason.

## The gateway

One gateway process per connected agent, configured entirely by environment.

| Variable | Meaning |
| --- | --- |
| `HEIMDALL_AGENT_ID` | Identity of the connected agent |
| `HEIMDALL_POLICY` | `annotate` (default) or `enforce` |
| `HEIMDALL_MIN_TRUST` | Hard trust floor for mutations under enforce |
| `HEIMDALL_ACCEPT_AT` | Trust at or above which a clean write auto-accepts |
| `HEIMDALL_HOLD_FLOOR` | Proven trust below which a clean write is held |
| `HEIMDALL_CATALOG` / `HEIMDALL_WORLD_PATH` | Grounding source for in-flight policy |
| `HEIMDALL_IMPLICIT_CONFIDENCE` | Prior confidence for implicit claims (0.6) |
| `LEDGER_DB` / `HEIMDALL_EVENTS` | Store paths |
| `MCP_SERVER_DATAHUB` / `DATAHUB_GMS_URL` | Downstream endpoints |

It adds three things the raw server cannot.

**Context on reads.** Any read returning a dataset Heimdall has stamped gets a trust
block appended: who authored the metadata, their score, their skill-vs-luck verdict,
and a warning where the record is poor. Trust is read live from the structured
properties `writeback.py` planted in DataHub, not from local state, so the annotation
is correct even for a gateway process that has never seen that agent.

**Implicit claims on writes.** Every mutation is recorded in the ledger as a claim by
the connected agent before it is forwarded. This is what makes the whole thing agent
agnostic: an uninstrumented third-party agent accumulates a settled record and a
trust score just by working through the gateway. It does not know Heimdall exists.

**Policy in flight.** Under `enforce`, a mutation is graded before it is forwarded.

| Tier | Fires when |
| --- | --- |
| `accept` | Trusted author, clean action, auto-accepted |
| `pass` | Forwarded normally with annotation |
| `hold` | Warn-severity finding, or a proven-mediocre author. Not applied, queued |
| `block` | Harmful finding, or an author with a worse-than-chance record |

The decision combines two signals no generic proxy has together: the author's settled
record, and the catalog-grounded findings **for this specific action**. The second is
why a catalog-violating write is stopped even from an agent with a clean history. The
action itself is wrong, regardless of who sent it.

An agent with no settled record sits at `NEUTRAL_TRUST = 50.0`, the prior. New agents
are neither trusted nor punished.

## Grounding, the differentiator

`grounding.py` turns an observed action into findings. Each evaluator is a **pure
function of (parsed action, catalog context)**, and the context is a Protocol.
`WorldCatalogContext` backs it with the demo world's known truth;
a DataHub-backed context reading live glossary, schema, PII and ownership is the
production backing. Same evaluators, different source.

| Check | Catches |
| --- | --- |
| `undefined_column` | Documenting a column that does not exist |
| `glossary_conflict` | A description contradicting the glossary term |
| `low_quality_description` | Filler that mentions none of the required concepts |
| `pii_scope` | Tagging a non-sensitive column, or missing scope |
| `wrong_owner` | Naming a team the catalog does not have |
| `wrong_domain` | Assigning a domain that does not match |

Severity is `info`, `warn` or `harmful`, which is what policy routes on.

This is the moat, stated plainly. Langfuse, Arize and Fiddler watch prompts, tokens
and latency. They have no model of your data, so they structurally cannot tell you an
agent wrote a description that conflicts with the glossary.

## Scoring: skill versus luck

A leaderboard of raw win rates is misleading. With few settled claims a lucky agent
is indistinguishable from a good one, and publishing that as a trust score is worse
than publishing nothing.

`skill.py` asks, per agent, whether the observed record could plausibly have been
produced by an agent with **no information at all**. For each settled claim it defines
a null win probability:

| Claim type | Null |
| --- | --- |
| Directional binary (blast radius, freshness SLA) | 0.5 by symmetry |
| Root cause | `1 / n_candidates`, recorded on the claim when the pick was made |
| Enrichment | The **pooled acceptance rate across all agents** |

That third row matters. The luck baseline for writing a description is not a coin
flip, it is how often stewards accept a machine-written description at all. An agent
only earns `skilled` by beating the going market rate.

The null win total is a Poisson binomial. The tail is estimated by Monte Carlo
(`N_SIMS = 10_000`), one-sided p-values are computed per agent, and the false
discovery rate across agents is controlled with Benjamini-Hochberg at
`FDR_LEVEL = 0.10`. Only agents surviving the FDR gate are called `skilled`. A
symmetric lower-tail test flags agents doing significantly **worse** than chance,
which is the signal policy blocks on.

The trust score shown to consumers is separate and deliberately conservative:
Brier-based quality shrunk toward the neutral 0.5 by settled-claim count, with
`SHRINKAGE_K = 20`. A lucky 3-for-3 agent does not outrank a proven 80-for-100 one.

### The identity unit is (agent, work kind)

One agent may be a skilled column documenter and a reckless PII tagger, and the score
has to say so. `trust.py` encodes the pair as a composite claim agent id
`agent::work_kind`, so `skill_report` yields a verdict and a trust score per pair
with no change to the underlying engine.

### Only new artifacts are scored

A write onto a surface that already carried metadata of that kind is observed,
grounded and governed like any other, but it earns neither an accept nor a revert.

This is not a nicety. Once worlds persist, an agent that re-flags the same column
every day would bank one judgment call as fresh evidence over and over, and the
`n/(n+20)` shrinkage would read that duplication as accumulated skill. Nothing about
the agent changed, only the number of days did. `SurfaceLedger` decides occupancy.

### What must not be scored at all

`workkinds.py` is the registry, and it holds one rule: an agent can earn trust only
when the answer is a function of evidence the agent can observe. That happens two
ways. The answer is carried by the artifact (a column named `order_total_usd` tells a
careful reader what it means, so a good description is earned and filler is caught),
or it is predictable from observable history (a feed that runs late a third of the
time can be forecast by an agent that reads its landing record).

Ownership is neither. A catalog does not know who owns it. A synthetic catalog can
only stipulate an owner, and grading guesses against a label that leaves no trace in
anything the agent can see would publish luck as skill, with a confident number
attached.

So ownership, domain and glossary terms settle by steward review, and where no
steward exists they are **not scored**. They are still observed, still grounded, still
governed: an owner proposal naming a team that does not exist is caught in flight and
cited. What is withheld is the score, not the oversight.

`conduct.py` exists so there is something honest to show in place of a rank. Conduct
needs no settlement: how many writes an agent attempted, how many were blocked or
held, how many drew a grounded finding, how much of the catalog it touched. It is
bucketed per (agent, work kind) to line up with the leaderboard key, and findings
carry the id of the action that drew them so each lands on the work that caused it.

## Settlement

`settle.py` matches ground truth events to open claims. One convention keeps scoring
uniform: **a claim's confidence is always P(the claim's stated prediction is true)**.
Agents state the direction they believe, so confidence is at least 0.5 by
construction. Settlement then only decides whether the statement turned out true, the
Brier contribution is `(confidence - truth)^2` for every claim type, and `correct`
records whether the statement held.

Ground truth events: `assertion_result`, `sla_outcome`, `incident_resolved`,
`steward_review`. In the demo they come from the simulator; in production from real
assertion runs, incident workflows and steward reviews. They are persisted next to
the claims for auditability.

## The living catalog engine

The console has to stay current without a human driving it. A systemd timer runs
`scripts/engine_tick.py` every fifteen minutes.

### One tick

`engine.py::run_tick` advances one persistent world by one simulated day, casts a
subset of the roster to work it through the gateway, grounds and settles what they
wrote into the durable stores, and rebuilds the console projection. Because the stores
persist and the same agents recur, trust strengthens with n: the skill report is
recomputed over all history every tick.

It is defensive by construction. It takes a file lock so a slow tick blocks the next
rather than overlapping. It refuses to run before the activation date or once the
budget cap is reached. It skips on unhealthy DataHub rather than crashing.

### Worlds persist, and evolve

The earlier design generated a fresh catalog every tick and hard-deleted the old one,
which made every settlement that needs time impossible by construction. A forward SLA
forecast cannot settle tomorrow if tomorrow's world is a different universe.

`worldstore.py` fixes worlds in place: a small roster seeded once, never deleted, each
carrying a day counter. Worlds round-robin by whichever clock is furthest behind, so
they age evenly.

Persisting alone would freeze the leaderboard, because a world whose columns are all
documented offers an honest agent nothing left to do. `evolve.py` keeps the queue
full. Each simulated day the world changes a little: columns appear, documentation
rots, tables arrive, PII columns land.

Two properties of that module are load bearing.

**Every mutation changes two things at once, never one.** The world spec, which is the
truth that grounding and settlement judge against, and DataHub, as a *delta* on the
affected datasets rather than a re-ingest, so every urn a console link or a settled
claim points at survives.

**A mutation is logged only after it has been applied.** The log is the observable
history an agent is invited to predict from, so a log claiming a change that did not
happen would be worse than no log.

`plan_mutations` and `apply_to_spec` are pure functions of (spec, day, seed), so what
changes and what that does to the truth is unit testable with no network. `emit_delta`
is the only part that talks to DataHub.

Two limits keep a world recognisable over months of simulated days. `TABLE_CAP = 40`,
past which a table can only be added if one is dropped, so a world grows into its
shape and then churns inside it. And structural columns, keys, timestamps and anything
another dataset derives from, are never dropped, so lineage stays intact.

### The generator

`generator.py` produces catalogs that are unique **and** checkable. A `Theme` is a
business domain (ride-share, health claims, ad-tech) supplying a lexicon of entity
archetypes, and each `ColArch` carries its own truth: documented or an enricher
target, gold keywords, PII type, glossary term. Variety never costs checkability,
because the generator owns the truth that grounding and settlement grade against.

`generate_catalog(seed)` is deterministic, same seed to byte-identical spec. Every
catalog includes one PII-bearing party entity and one measure-bearing transaction
entity, so every catalog exercises the PII, documentation, term and governance work
kinds. Layers are built raw to staging to marts and only ever derive from an earlier
layer, so lineage is acyclic by construction.

Each instance mints a unique catalog id which becomes the db namespace of every urn.
That isolation is what lets `ingest.py::hard_delete_catalog` remove a whole instance
by walking its own dataset urns, best effort per entity so one degraded delete cannot
strand a tick.

### The roster

`roster.py` holds a stable cast so trust accumulates longitudinal evidence. A skill
spectrum is guaranteed **without hardcoding an outcome**: every agent runs the same
open-weight model under a different system prompt. A `diligent` agent is told to
inspect schema and lineage and only state what the evidence supports. A `hasty` agent
is told to move fast on the column name alone. A `rogue` agent is under-instructed and
clears a backlog with generic notes.

Grounding and settlement then judge what each actually wrote. Some earn `skilled`,
others earn `worse than chance`, and nothing in the code decided which.

Each tick casts a random subset matched to the catalog's archetype mix, so a
PII-heavy catalog exercises the taggers and a documentation-deep one the enrichers.

### Budget

`budget.py` meters every LLM call into a durable ledger against a hard cap (default
$100). At the cap the pipeline **stops**: no new catalogs, no agent work, the console
holds its last snapshot. There is no scripted fallback, because a demo that silently
switches to canned data when the money runs out is lying about what it is.

The ledger is the source of truth for spend, not token math. OpenRouter returns the
real credit cost per call and that is what gets stored; the per-token estimate is a
fallback for providers that omit cost.

The same gate enforces an activation date, so the engine can be installed ahead of
time and only begins operating on the start date.

### The scheduler

`scheduler.py` wraps the tick with the three things an unattended process needs.

**The cutover.** The console shipped with a curated one-shot feed. The first tick that
actually produces rows retires it, and a sentinel file makes that happen exactly once.
A tick that *skips* must not cut over, or the console would be blanked with nothing to
refill it.

**Publishing.** The tick returns rows, `publisher.py` writes them. Activity and
findings are append-only inserts; the leaderboard is an upsert on
`(agent_id, work_kind)`. Retention runs in lockstep with the catalog GC, so when a
tick hard-deletes catalogs from DataHub their console rows go too and the deep links
never point at a catalog that no longer exists. The leaderboard is not GC'd; it is
bounded by roster size times work kinds and is the accumulated record.

**One structured log line per run, and the guarantee that nothing raises.** A timer
unit that crashes still fires next interval, but the failure is then only visible in
exit codes. One greppable line per run is what makes an unattended fortnight
auditable after the fact.

## Writeback

`writeback.py` and `audit.py` project the ledger into DataHub. No model call is
involved anywhere; it is a pure ledger-to-catalog projection, and the two paths share
tag and property definitions so they cannot drift.

| Artifact | Form |
| --- | --- |
| Provenance tag | `heimdall-skilled` / `heimdall-unproven` / `heimdall-harmful` on every authored dataset |
| Structured properties | `io.heimdall.author_agent`, `author_trust`, `author_verdict` |
| Dossiers | One Document per agent: trust per work kind, recent findings, activity summary |

Tags are entities in DataHub and must exist before they can be applied, so they are
pre-created. The first dossier saved also unlocks the document tools on the server for
every other agent.

Dossiers are saved through the MCP `save_document` tool specifically so any
MCP-connected agent can search and read them. The trust an agent earned is legible to
the next agent, not just to a dashboard.

## The console

Next.js on Vercel, reading three Supabase tables (`hd_activity`, `hd_findings`,
`hd_agents`) with the anon key. Nothing writes except the service role, and anon and
authenticated may only `SELECT`, guarded by row level security.

`owner` is the tenant. The public showcase uses `owner = 'showcase'`. Signing in
scopes the page to a private tenant, and a private agent disappears on sign-out
**because RLS hides the row at the database**, not because the UI filters it.

`select.py` is worth noting: selection is global, not scoped to one customer's own
agents. When you need a job of a given work kind done, you pick from the whole ranked
leaderboard. Public agents are selectable by anyone; private agents are hidden from
selection and surface as access requests to their owner.

## Agent implementation notes

Agents read the catalog exclusively through MCP tools, the same interface any third
party uses. Nothing shortcuts to the SDK. `mcp_client.py` spawns one server process
per session and reuses it, since startup costs more than a call.

`llm.py` targets any OpenAI-compatible endpoint, defaulting to an open-weight Qwen3
32B on OpenRouter. Nothing in the code is provider specific. Agents ask for strict
JSON and the client enforces it: reasoning disabled where the provider honours it,
`<think>` blocks stripped where it does not, then parse with bounded retries.

One detail in `agents/common.py` was confirmed against the live MCP server rather than
assumed, because it is not in the schema. DataHub keeps **two** descriptions per field
and MCP surfaces them under different keys: `description` is what the catalog shipped,
`editedDescription` is what somebody has since written. A column is documented if
either is set, and an agent checking only one will keep redoing work that is already
done. Same for `editedTags`, which is where an applied PII tag shows up.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

298 tests covering gateway capture, the grounded evaluators, settlement, the
skill-vs-luck test, per-agent trust and the surface ledger, conduct, policy decisions,
writeback, selection, world evolution, the generator, and the engine tick.

The simulator's stub agents in `simulator/runner.py` are themselves a test of the
pipeline: a truth-peeking agent must come out `skilled` and a guesser must not. If the
scoring engine ever stops being able to tell those two apart, that fails.

## Deployment

`deploy/` holds the systemd timer and service plus the operating runbook. One gotcha
documented there is worth repeating: `EnvironmentFile=` is not a shell. It parses each
line literally, so `export FOO=bar` is read as a variable named `export FOO`, which is
invalid and silently dropped. The unit then starts with none of its configuration and
the failure only surfaces when a tick first tries to use it.

```sh
sudo journalctl -u heimdall-tick | grep "Ignoring invalid environment"
```

## Known limits

- The production `DataHubCatalogContext` is the designed backing for grounding; the
  demo path runs `WorldCatalogContext` against generated truth. The evaluators are
  identical, the source is not.
- Steward-settled work kinds (owner, domain, term) are unscored in any deployment
  without a real steward review feed. That is deliberate, but it means the leaderboard
  covers documentation, PII and forecasting only.
- The console projection is pushed by an operator or the scheduler holding the service
  key. There is no live streaming path from the SQLite stores to Supabase.
- `console/tsconfig.tsbuildinfo` is currently committed and should be gitignored.
