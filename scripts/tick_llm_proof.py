"""T4 live proof: one metered LLM tick on a generated catalog.

Generates a fresh catalog, ingests it, then runs three stable column-doc agents
through the gateway against it: a diligent, a hasty, and a rogue profile of the
same open-weight model. Every LLM call is metered into the spend ledger. Their
writes are observed, grounded against the generated truth, and settled into the
skill engine. The proof: real LLM-authored metadata is graded, the diligent agent
out-trusts the rogue one (the spectrum is earned, not scripted), and cost is
recorded per agent. The catalog is hard-deleted at the end.

Run on the box:
    ~/fresh-e2e/v/bin/python scripts/tick_llm_proof.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

from heimdall.agents.enricher import EnricherAgent
from heimdall.budget import SpendLedger
from heimdall.catalog import spec_to_world
from heimdall.generator import generate_catalog
from heimdall.grounding import WorldCatalogContext
from heimdall.ingest import hard_delete_catalog, ingest_spec
from heimdall.llm import DEFAULT_MODEL, LLMClient
from heimdall.mcp_client import DataHubMCP
from heimdall.observability import EventStore
from heimdall.roster import PROFILE_SYSTEMS
from heimdall.simulator.steward import KIND_COLUMN_DOC
from heimdall.skill import HARMFUL, SKILLED
from heimdall.trust import score_events

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable
MODEL = os.environ.get("LLM_MODEL", DEFAULT_MODEL)

# the three column-doc profiles from the stable roster
AGENTS = [("atlas-doc", "diligent"), ("juno-doc", "hasty"), ("nyx-doc", "rogue")]


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"FATAL: env {name} required", file=sys.stderr)
        sys.exit(2)
    return v


def gateway(agent_id: str, events_db: str, ledger_db: str, mcp_server: str) -> DataHubMCP:
    return DataHubMCP(
        gms_url=GMS, command=PYEXE, args=["-m", "heimdall.gateway"],
        extra_env={
            "HEIMDALL_AGENT_ID": agent_id, "HEIMDALL_EVENTS": events_db,
            "LEDGER_DB": ledger_db, "HEIMDALL_POLICY": "annotate",
            "MCP_SERVER_DATAHUB": mcp_server, "DATAHUB_GMS_URL": GMS,
        },
    )


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    _require("OPENROUTER_API_KEY")
    tmp = tempfile.mkdtemp(prefix="heimdall-t4-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")
    spend = SpendLedger(os.path.join(tmp, "spend.db"))

    seed = int(time.time())
    spec = generate_catalog(seed)
    world = spec_to_world(spec)
    raw_urns = [world.datasets[d.name].urn for d in spec.datasets if d.name.startswith("raw_")]
    print(f"catalog={spec.catalog} theme={spec.theme} platform={spec.platform} "
          f"raw_datasets={len(raw_urns)} seed={seed}")

    n_mcps = ingest_spec(spec, gms_url=GMS)
    print(f"ingested {n_mcps} MCPs")

    for agent_id, profile in AGENTS:
        llm = LLMClient(model=MODEL, usage_sink=spend.usage_sink(agent_id, MODEL))
        applied = 0
        with gateway(agent_id, events_db, ledger_db, mcp_server) as mcp:
            agent = EnricherAgent(mcp, llm, agent_id=agent_id, system=PROFILE_SYSTEMS[profile])
            for urn in raw_urns:
                try:
                    for claim in agent.propose(urn):
                        try:
                            agent.apply(claim)
                            applied += 1
                        except RuntimeError as exc:
                            print(f"  ({agent_id} write sent, downstream: {str(exc)[:50]})")
                except Exception as exc:
                    print(f"  ({agent_id} propose on {urn.split(',')[1]} failed: {str(exc)[:60]})")
        llm.close()
        print(f"{agent_id:10} ({profile:8}) applied {applied} descriptions, "
              f"spent ${spend.by_agent().get(agent_id, 0):.4f}")

    # settle observations against the generated truth
    events = EventStore(events_db).events()
    ctx = WorldCatalogContext(world)
    counts, report = score_events(events, ctx, os.path.join(tmp, "trust.db"))
    print(f"\nsettlement: {counts}")

    def rec(agent_id: str) -> dict:
        return report.get(agent_id, {}).get(KIND_COLUMN_DOC, {})

    print("\n=== column-doc trust ===")
    rows = sorted(
        ((a, rec(a)) for a, _ in AGENTS),
        key=lambda r: r[1].get("trust", 0), reverse=True,
    )
    for agent_id, r in rows:
        print(f"  {agent_id:10} trust {r.get('trust', 0):5}  {r.get('verdict', 'n/a'):22} "
              f"({r.get('wins', 0)}/{r.get('n_settled', 0)} accepted)")

    diligent, rogue = rec("atlas-doc"), rec("nyx-doc")
    total_spend = spend.total()
    checks = [
        ("catalog ingested", n_mcps > 0),
        ("spend recorded for every agent", all(a in spend.by_agent() for a, _ in AGENTS)),
        ("total spend is positive", total_spend > 0),
        ("column docs were settled", sum(v for k, v in counts.items()) > 0),
        ("diligent agent was scored", diligent.get("n_settled", 0) >= 1),
        ("rogue agent was scored", rogue.get("n_settled", 0) >= 1),
        ("diligent out-trusts rogue", diligent.get("trust", 0) > rogue.get("trust", 0)),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print(f"\ntotal spend this tick: ${total_spend:.4f} over {spend.calls()} calls")

    print("\ncleanup: hard-deleting the catalog")
    results = hard_delete_catalog(spec, gms_url=GMS)
    gone = sum(1 for r in results if r.ok)
    print(f"deleted {gone}/{len(results)} datasets")

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
