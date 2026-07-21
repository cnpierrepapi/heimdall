#!/usr/bin/env python
"""B2 live proof: a global leaderboard by work_kind, ready to publish.

Three agents document the same columns through the gateway with different
skill: an expert (all correct), a middling agent (half right), and a rogue (all
filler). Every write is observed, grounded, and settled (A1/A2/B1). Then the
trust engine ranks them per work_kind and picks the best agent for each kind.

This is the SELECT surface: given a job of a certain kind, point it at the agent
that has earned the most trust doing exactly that kind of work. The script
emits the hd_agents rows as JSON so the operator can publish them to the public
table the console reads. Exit 0 only if the ranking is right. Catalog reverted.

Run on the box:  ~/fresh-e2e/v/bin/python scripts/leaderboard_session.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from heimdall.grounding import WorldCatalogContext
from heimdall.mcp_client import DataHubMCP
from heimdall.observability import EventStore
from heimdall.simulator.steward import KIND_COLUMN_DOC
from heimdall.simulator.world import build_default_world
from heimdall.trust import (
    best_agent_per_kind,
    hd_agents_rows,
    leaderboard,
    settle_observations,
)
from heimdall.claims import ClaimStore

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable

COLS = [
    ("raw_orders", "order_total_usd", "Total order amount in usd."),
    ("raw_orders", "discount_code", "Promotional discount coupon code."),
    ("raw_payments", "amount_usd", "Amount paid in usd, once settled."),
    ("raw_customers", "email", "Customer email address."),
    ("raw_customers", "country_code", "Customer country iso code."),
    ("raw_web_events", "event_type", "Type of the web event action."),
]


def _require(name):
    v = os.environ.get(name)
    if not v:
        print(f"FATAL: env {name} required", file=sys.stderr)
        sys.exit(2)
    return v


def client(agent_id, events_db, ledger_db, mcp_server):
    return DataHubMCP(
        gms_url=GMS, command=PYEXE, args=["-m", "heimdall.gateway"],
        extra_env={"HEIMDALL_AGENT_ID": agent_id, "HEIMDALL_EVENTS": events_db,
                   "LEDGER_DB": ledger_db, "HEIMDALL_POLICY": "annotate",
                   "MCP_SERVER_DATAHUB": mcp_server, "DATAHUB_GMS_URL": GMS})


def document(agent_id, n_correct, world, events_db, ledger_db, mcp_server):
    with client(agent_id, events_db, ledger_db, mcp_server) as mcp:
        for i, (ds, col, desc) in enumerate(COLS):
            text = desc if i < n_correct else "a column here"
            try:
                mcp.call("update_description", {
                    "entity_urn": world.datasets[ds].urn, "column_path": col,
                    "description": text, "operation": "replace"})
            except RuntimeError:
                pass


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    tmp = tempfile.mkdtemp(prefix="heimdall-b2-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")
    world = build_default_world()

    print("three agents document the same columns with different skill")
    document("expert-doc", 6, world, events_db, ledger_db, mcp_server)
    document("mid-doc", 3, world, events_db, ledger_db, mcp_server)
    document("rogue-doc", 0, world, events_db, ledger_db, mcp_server)

    print("cleanup: reverting writes")
    with client("cleanup", events_db, ledger_db, mcp_server) as mcp:
        for ds, col, _ in COLS:
            try:
                mcp.call("update_description", {"entity_urn": world.datasets[ds].urn,
                                                "column_path": col, "operation": "remove"})
            except RuntimeError:
                pass

    events = EventStore(events_db).events()
    ctx = WorldCatalogContext(world)
    store = ClaimStore(os.path.join(tmp, "trust.db"))
    settle_observations(events, ctx, store)

    board = leaderboard(store, KIND_COLUMN_DOC)
    best = best_agent_per_kind(store)
    rows = hd_agents_rows(store)

    print("\n=== global leaderboard: column documentation ===")
    for r in board:
        print(f"  {r['agent_id']:12} trust {r['trust']:5}  {r['verdict']:28} "
              f"({r['wins']}/{r['n_settled']} accepted)")
    print(f"\nbest agent for column_doc: {best.get(KIND_COLUMN_DOC, {}).get('agent_id')}")
    print("\nHD_AGENTS_JSON=" + json.dumps(rows))

    names = [r["agent_id"] for r in board]
    checks = [
        ("three agents ranked", len(board) == 3),
        ("expert ranked first", names[0] == "expert-doc"),
        ("rogue ranked last", names[-1] == "rogue-doc"),
        ("best-agent selection picks the expert", best.get(KIND_COLUMN_DOC, {}).get("agent_id") == "expert-doc"),
        ("rogue is worse than chance", any(r["agent_id"] == "rogue-doc"
             and r["verdict"] == "worse than chance" for r in board)),
        ("hd_agents rows carry work_kind", all("work_kind" in r for r in rows)),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
