"""Run the blast-radius agent against live DataHub, settle against truth.

The agent reads lineage and schemas through the MCP server only. Truth
comes from the simulator world (which was ingested into this DataHub
instance by ingest_world.py), so the two scenario schema changes settle
immediately and print a per-claim scorecard.

Run on the box:
  OPENROUTER_API_KEY=... ~/dh/bin/python scripts/run_blast_demo.py
"""

from __future__ import annotations

import os
import sys
import time

from heimdall.agents import BlastRadiusAgent
from heimdall.claims import ClaimStore
from heimdall.llm import LLMClient
from heimdall.mcp_client import DataHubMCP
from heimdall.settle import (
    ASSERTION_RESULT,
    GroundTruthEvent,
    SettlementEngine,
    agent_stats,
)
from heimdall.skill import trust_score
from heimdall.simulator.world import build_default_world

SCENARIO = [
    ("raw_orders", "discount_code"),
    ("raw_customers", "email"),
]

DB_PATH = os.environ.get("LEDGER_DB", os.path.expanduser("~/heimdall-demo.db"))


def main() -> None:
    world = build_default_world()
    llm = LLMClient()
    n_correct = 0
    n_total = 0

    with ClaimStore(DB_PATH) as store, DataHubMCP() as mcp:
        engine = SettlementEngine(store)
        agent = BlastRadiusAgent(mcp, llm, agent_id="blast-radius-live")

        for dataset, column in SCENARIO:
            changed = world.datasets[dataset]
            blast = world.blast_set(dataset, column)
            print(f"\n=== drop {dataset}.{column} (true blast: {sorted(blast)}) ===")

            claims = agent.forecast(changed.urn, column)
            if not claims:
                print("!! no downstream candidates found via MCP lineage")
                sys.exit(1)
            for c in claims:
                store.record(c)

            # settle from world truth
            now = time.time() + 1
            for c in claims:
                name = world.by_urn(c.entity_urn).name
                engine.process_event(
                    GroundTruthEvent(
                        event_type=ASSERTION_RESULT,
                        entity_urn=c.entity_urn,
                        payload={"passed": name not in blast},
                        ts=now,
                    )
                )

            for c in store.claims(settled=True):
                if c.prediction.get("dropped_column") != column:
                    continue
                name = world.by_urn(c.entity_urn).name
                verdict = "RIGHT" if c.correct else "WRONG"
                n_total += 1
                n_correct += bool(c.correct)
                print(
                    f"  {verdict:5s} {name:16s} predicted "
                    f"{'break' if c.prediction['will_break'] else 'survive':8s} "
                    f"p={c.confidence:.2f} | {c.prediction.get('reason','')[:80]}"
                )

        stats = agent_stats(store)[agent.agent_id]
        settled = store.claims(agent_id=agent.agent_id, settled=True)
        print(
            f"\nscore: {n_correct}/{n_total} correct | "
            f"brier={stats['brier_mean']:.3f} | trust={trust_score(settled)}"
        )
        engine.close()
    llm.close()


if __name__ == "__main__":
    main()
