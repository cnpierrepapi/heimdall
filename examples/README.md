# Run the whole loop yourself

This walkthrough takes an empty DataHub to a fully scored agent fleet: real
catalog work, falsifiable claims, settlement against ground truth, trust
written back into DataHub, and a gateway that blocks the agent that earned
distrust. Every stage is a script you can run and verify.

## Prerequisites

- A running DataHub with the MCP server. The demo setup is the standard
  quickstart plus [mcp-server-datahub](https://github.com/acryldata/mcp-server-datahub):

  ```bash
  datahub docker quickstart
  # in the venv where the datahub CLI lives:
  pip install mcp-server-datahub
  export TOOLS_IS_MUTATION_ENABLED=true   # enable write tools on OSS
  ```

- Python 3.11+.
- Any OpenAI-compatible model endpoint. The default stack is open weight
  (Qwen3 32B via OpenRouter); nothing in the code is provider specific.

## Environment

| Variable | Meaning | Demo value |
| --- | --- | --- |
| `LLM_BASE_URL` | OpenAI-compatible endpoint | `https://openrouter.ai/api/v1` |
| `LLM_MODEL` | model id | `qwen/qwen3-32b` |
| `LLM_API_KEY` or `OPENROUTER_API_KEY` | endpoint key | your key |
| `MCP_SERVER_DATAHUB` | path to the `mcp-server-datahub` binary | `~/dh/bin/mcp-server-datahub` |
| `DATAHUB_GMS_URL` | DataHub GMS | `http://localhost:8080` |
| `LEDGER_DB` | where the claim ledger lives | `~/heimdall-demo.db` |

## One shot

```bash
bash examples/run_all.sh
```

Creates a fresh venv, installs the package, and runs stages 1 through 4
below. Ends with `ALL STAGES PASSED` or a nonzero exit.

## Stage by stage

### 1. Seed the world

```bash
python scripts/ingest_world.py
```

Ingests `lineworld`, a 12-dataset warehouse graph with column-level lineage,
into your DataHub. This is the sandbox the agents work in; the datasets,
lineage, and schemas are real catalog entities.

### 2. Agents do real work, claims settle

```bash
python scripts/run_agents_demo.py
```

Four agents work the catalog through DataHub MCP: the blast-radius
forecaster, the freshness sentinel, the enricher, and incident triage. Every
action is recorded as a claim with stated confidence, ground truth events
settle them, and the run ends with a per-agent skill report (win rate,
Brier, skill-vs-luck verdict, trust).

### 3. Trust lands in DataHub

```bash
python scripts/run_writeback.py
```

Projects the settled ledger back into the catalog: provenance tags,
structured trust properties, accepted descriptions applied, and a dossier
document per agent. The script verifies everything through a fresh MCP
session afterwards.

Now open the DataHub UI and look at any authored dataset (for the demo
world, `lineworld.raw_orders` is a good one):

- the author's skill-vs-luck verdict is a badge next to the dataset name,
- the sidebar summary shows Heimdall author agent, trust, and verdict,
- columns carry the agent-written descriptions, marked as edited,
- the `heimdall-*` tag states the author's standing,
- search for "dossier" under Documents to read each agent's full record.

### 4. The gateway earns its keep

```bash
python scripts/gateway_e2e.py
```

Self-asserting end-to-end proof, nonzero exit on any failure: an
uninstrumented rogue agent works through the gateway, its write is recorded
as an implicit claim, a steward review settles it wrong, the bad edit is
reverted, and the rogue's next write is rejected at the trust floor while
reads keep working.

To put the gateway in front of your own agent, point the agent's MCP client
at heimdall instead of the raw server:

```bash
HEIMDALL_AGENT_ID=my-agent \
HEIMDALL_POLICY=enforce \
HEIMDALL_MIN_TRUST=55 \
python -m heimdall.gateway
```

### 5. Publish the public board (optional)

```bash
SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python scripts/publish_scoreboard.py
```

Pushes the ledger projections to the public scoreboard. The live instance
for this repo is https://heimdall-scoreboard.vercel.app.
