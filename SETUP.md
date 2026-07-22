# Evaluating Heimdall

Three ways to evaluate this project, fastest first. A judge can confirm the
whole idea in under a minute with track 1, verify the scoring core runs from a
clean clone with track 2, and reproduce the full live loop against a real
DataHub with track 3.

---

## Track 1: look at the live console (no setup)

Open **https://heimdall-tech.vercel.app**.

Everything on the page is live from a real DataHub catalog (`lineworld`), read
through row-level-security-guarded tables. What to look at:

- **Live activity**: every agent call observed at the gateway, reads and writes,
  including writes that were **held** or **blocked** by policy in flight.
- **Leaderboard**: trust by work kind. Each agent is scored skill-vs-luck, so a
  lucky agent does not outrank a proven one. The top public agent per work kind
  is marked selected.
- **Catalog-grounded findings**: each one cites the catalog fact it broke (a
  glossary conflict, a column that does not exist, PII tagged out of scope) and
  deep-links to the dataset in DataHub.
- Click any agent id to open its **trust report**.

### Sign in as a tenant (see per-tenant isolation)

The console is public, but signing in scopes it to a private tenant. Use the
read-only judge demo account:

- email: `acme-demo@heimdall.tech`
- password: `heimdall-demo`

After signing in you will see the `acme` tenant: its own activity, its own
findings, and its private agent `acme-internal-doc` with full scores. Sign out
and that private agent disappears, because private rows are hidden at the
database by row level security, not just in the UI. This is the multi-tenant
control plane from the consumer seat.

---

## Track 2: run the scoring core locally (zero services)

The scoring engine is pure Python and runs from a clean clone with no external
services. This verifies install and the whole claim -> settle -> skill-vs-luck
-> trust path.

Use Python 3.11 or 3.12. The dependency stack (pydantic, acryl-datahub) ships
prebuilt wheels for those; on Python 3.13+ pip may try to compile pydantic-core
from source and fail unless you have a Rust toolchain.

```bash
git clone https://github.com/cnpierrepapi/heimdall
cd heimdall
python3.12 -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
python -m pip install -U pip
pip install -e ".[dev]"
pytest -q
```

Expected: the full suite passes (167 tests today). These cover the gateway
capture, the catalog-grounded evaluators, settlement, the skill-vs-luck test,
per-agent trust, policy decisions, writeback, and selection.

---

## Track 3: run the full live loop against your own DataHub

This drives real agents through the gateway into a real DataHub, settles their
claims, writes trust back into the catalog, and proves the gateway blocks the
agent that earned distrust.

### Prerequisites

- A running DataHub with the MCP server:

  ```bash
  datahub docker quickstart
  # in the venv where the datahub CLI lives:
  pip install mcp-server-datahub
  export TOOLS_IS_MUTATION_ENABLED=true   # enable write tools on OSS
  ```

- Python 3.11+.
- Any OpenAI-compatible model endpoint. The default stack is open weight
  (Qwen3 32B via OpenRouter); nothing in the code is provider specific.

### Environment

Set the variables from the table in [examples/README.md](examples/README.md)
(`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `MCP_SERVER_DATAHUB`,
`DATAHUB_GMS_URL`, `LEDGER_DB`).

### Run it

```bash
bash examples/run_all.sh
```

This seeds a 12-dataset warehouse graph, runs four agents through the gateway,
settles their claims, writes trust back into DataHub, and proves the gateway at
the end. It exits `ALL STAGES PASSED` or nonzero. Each stage is also a standalone
script; [examples/README.md](examples/README.md) walks them one at a time,
including what to open in the DataHub UI afterward.

### What judge permissions mean here

To see the writeback in the DataHub UI (the author trust badge on a dataset, the
`heimdall-*` provenance tags, the structured properties in the sidebar, and the
per-agent dossiers under Documents), sign in to your DataHub as a user who can
view metadata and documents. On the OSS quickstart the default `datahub` admin
account has everything needed. No special Heimdall permission exists; Heimdall
reads and writes through the same MCP tools any agent uses.

---

## Publishing the console projection (optional)

The console reads three tables (`hd_activity`, `hd_findings`, `hd_agents`);
their schema and row level security are in
[console/supabase/schema.sql](console/supabase/schema.sql).
`scripts/publish_snapshot.py` produces the rows from a live session for an
operator to load with the service role key.
