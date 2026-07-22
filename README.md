# Heimdall

**The agent observability and trust control plane for [DataHub](https://datahub.com).**

DataHub has become the context platform for AI agents: it sits between your agents and your data, serving catalog context so agents act accurately. But it does not watch the agents back. Nobody is measuring whether an agent used that context correctly, which agent is best for which job, or whether an agent just wrote something that contradicts your glossary or touched PII it had no business touching.

Heimdall closes that loop. It sits in the agent to DataHub path as an MCP gateway and:

- **Observes** every agent interaction live, reads and writes, with the agent's identity attached.
- **Grounds** each action in catalog context (glossary, schema, lineage, PII, ownership) to produce findings a generic LLM tracer structurally cannot: this write contradicts a glossary term, this used a deprecated column, this description does not match the schema, this touched PII out of scope.
- **Scores** each agent's reliability per kind of work, separating skill from luck with a heavy tail robust statistical test.
- **Governs** by policy at the gateway: auto accept high trust actions, hold suspicious ones, block harmful ones.
- **Writes trust and audit back** into DataHub so the next agent inherits not just the metadata but the reliability of whoever wrote it.

It answers four questions about the agents running against your catalog: what are they doing, are they any good, which is best for which task, and are they safe.

## Why catalog grounded

Generic agent tracers (Langfuse, Arize, Fiddler) watch prompts, tokens, and latency. They have no model of your data, so they cannot tell you an agent wrote a description that conflicts with the glossary. Heimdall observes from inside the DataHub path, so every finding is grounded in the catalog the agent is acting on. That grounding is the moat.

## Components

- **Observability gateway**: a drop in MCP proxy that mirrors DataHub's MCP tools and captures every call as a structured observation event.
- **Grounded evaluators**: per action checks against catalog context that emit specific, cited findings.
- **Trust engine**: settled outcomes accumulate into per agent, per work kind reliability scores with calibration and a skill versus luck verdict.
- **Policy layer**: threshold routing at the gateway (auto accept, hold, block).
- **Writeback**: trust tags, structured properties, and agent dossiers projected back into DataHub.
- **Console**: a live activity feed, per agent trust, grounded findings, leaderboard, and policy actions.

## Runtime

Fully open stack. The scoring engine is pure Python. The agent facing LLM runs on any OpenAI compatible endpoint (default an open weight Qwen model); no proprietary model is required.

## Quickstart

You need a running DataHub with the MCP server, Python 3.11+, and any
OpenAI-compatible model endpoint (the default stack is an open-weight Qwen; no
proprietary model is required). Full prerequisites and the environment table are
in [examples/README.md](examples/README.md).

The whole loop runs from a clean checkout with one command:

```bash
bash examples/run_all.sh
```

That seeds a 12-dataset warehouse graph into DataHub, turns four agents loose on
it through the gateway, settles their claims against ground truth, writes the
earned trust back into the catalog, and proves the gateway blocks the agent that
earned distrust while reads keep working. It ends with `ALL STAGES PASSED` or a
nonzero exit. Each stage is also a script you can run and inspect on its own;
the walkthrough covers them one at a time.

To put the gateway in front of your own agent, point its MCP client at heimdall
instead of the raw server:

```bash
HEIMDALL_AGENT_ID=my-agent \
HEIMDALL_POLICY=enforce \
HEIMDALL_MIN_TRUST=55 \
python -m heimdall.gateway
```

The agent keeps the identical tool surface. It now also accumulates a settled
record, gets its writes graded against the catalog in flight, and inherits the
trust of whoever authored the metadata it reads.

## Console

A live console reads the public projection of the ledger: the activity feed,
the leaderboard by work kind, and the grounded findings with deep links back
into DataHub. It is at [heimdall-tech.vercel.app](https://heimdall-tech.vercel.app).
The tables it reads and their row-level security are in
[console/supabase/schema.sql](console/supabase/schema.sql);
`scripts/publish_snapshot.py` produces the rows.

## Repository layout

```
heimdall/          the package
  gateway.py         the MCP trust gateway (observe, ground, govern, annotate)
  observability.py   the event store: every observed tool call
  grounding.py       catalog-grounded evaluators (the moat)
  claims.py          the claim ledger; settle.py settles against ground truth
  skill.py           skill-vs-luck decomposition and trust scores
  trust.py           per-(agent, work_kind) scoring; select.py picks the best
  policy.py          accept / pass / hold / block decisions
  writeback.py       projects trust and dossiers back into DataHub
  snapshot.py        builds the console projection
  simulator/         the demo world and its ground truth
examples/          the end-to-end walkthrough
scripts/           runnable stages and proofs
console/           the Next.js console
tests/             the test suite
```

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## License

Apache 2.0
