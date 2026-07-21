#!/usr/bin/env python
"""B1 live proof: score agents skill-vs-luck from what they wrote.

Two agents document columns through the gateway against the live DataHub on the
box. The good agent writes descriptions that match each column's meaning; the
rogue agent writes filler. Every write is observed (A1) and grounded (A2), then
settled into the skill engine (B1): a grounded violation is a revert, a clean
write on a judgeable column is an accept. The result is a trust score and a
skill-vs-luck verdict per (agent x work_kind).

The point: with the same number of writes, the good agent reads as "skilled"
(beats the pooled acceptance baseline) and the rogue agent as "worse than
chance", not merely a raw win-rate gap. Exit 0 only if that holds. Writes are
reverted so the demo catalog stays clean.

Run on the box:  ~/fresh-e2e/v/bin/python scripts/score_session.py
"""

from __future__ import annotations

import os
import sys
import tempfile

from heimdall.grounding import WorldCatalogContext
from heimdall.mcp_client import DataHubMCP
from heimdall.observability import EventStore
from heimdall.simulator.steward import KIND_COLUMN_DOC
from heimdall.simulator.world import build_default_world
from heimdall.skill import HARMFUL, SKILLED
from heimdall.trust import score_events

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable

# columns that carry gold keywords, so a description is gradeable
COLS = [
    ("raw_orders", "order_total_usd", "Total order amount in usd."),
    ("raw_orders", "discount_code", "Promotional discount coupon code."),
    ("raw_payments", "amount_usd", "Amount paid in usd, once settled."),
    ("raw_customers", "email", "Customer email address."),
    ("raw_customers", "country_code", "Customer country iso code."),
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
        extra_env={
            "HEIMDALL_AGENT_ID": agent_id, "HEIMDALL_EVENTS": events_db,
            "LEDGER_DB": ledger_db, "HEIMDALL_POLICY": "annotate",
            "MCP_SERVER_DATAHUB": mcp_server, "DATAHUB_GMS_URL": GMS,
        },
    )


def write(mcp, world, ds, col, desc):
    try:
        mcp.call("update_description", {
            "entity_urn": world.datasets[ds].urn, "column_path": col,
            "description": desc, "operation": "replace"})
    except RuntimeError as exc:
        print(f"  (write sent, downstream: {str(exc)[:60]})")


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    tmp = tempfile.mkdtemp(prefix="heimdall-b1-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")
    world = build_default_world()

    print("good-agent documents columns correctly")
    with client("good-agent", events_db, ledger_db, mcp_server) as mcp:
        for ds, col, desc in COLS:
            write(mcp, world, ds, col, desc)

    print("rogue-agent writes filler")
    with client("rogue-agent", events_db, ledger_db, mcp_server) as mcp:
        for ds, col, _ in COLS:
            write(mcp, world, ds, col, "a column here")

    print("cleanup: reverting writes")
    with client("cleanup", events_db, ledger_db, mcp_server) as mcp:
        for ds, col, _ in COLS:
            try:
                mcp.call("update_description", {
                    "entity_urn": world.datasets[ds].urn,
                    "column_path": col, "operation": "remove"})
            except RuntimeError:
                pass

    # settle observations into the skill engine and score
    events = EventStore(events_db).events()
    ctx = WorldCatalogContext(build_default_world())
    counts, report = score_events(events, ctx, os.path.join(tmp, "trust.db"))
    print(f"\nsettlement: {counts}")

    board = leaderboard_from(report)
    print("\n=== trust leaderboard: column documentation ===")
    for agent, rec in board:
        print(f"  {agent:12} trust {rec['trust']:5}  {rec['verdict']:28} "
              f"({rec['wins']}/{rec['n_settled']} accepted)")

    good = report.get("good-agent", {}).get(KIND_COLUMN_DOC, {})
    rogue = report.get("rogue-agent", {}).get(KIND_COLUMN_DOC, {})
    checks = [
        ("good agent scored on column docs", bool(good) and good.get("n_settled", 0) >= 5),
        ("rogue agent scored on column docs", bool(rogue) and rogue.get("n_settled", 0) >= 5),
        ("good agent verdict is skilled", good.get("verdict") == SKILLED),
        ("rogue agent verdict is worse than chance", rogue.get("verdict") == HARMFUL),
        ("good agent out-trusts rogue", good.get("trust", 0) > rogue.get("trust", 100)),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def leaderboard_from(report):
    rows = []
    for agent, kinds in report.items():
        if KIND_COLUMN_DOC in kinds:
            rows.append((agent, kinds[KIND_COLUMN_DOC]))
    rows.sort(key=lambda r: r[1].get("trust", 0.0), reverse=True)
    return rows


if __name__ == "__main__":
    sys.exit(main())
