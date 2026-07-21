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

## Status

Under active development for the DataHub Agent Hackathon. Setup instructions, examples, and a demo walkthrough land here as components ship.

## License

Apache 2.0
