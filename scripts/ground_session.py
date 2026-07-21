#!/usr/bin/env python
"""A2 live proof: catch a misbehaving agent with catalog-grounded reasons.

Two agents work through the heimdall gateway against the live DataHub on the
box. A good agent writes correct descriptions. A rogue agent writes metadata
that violates the catalog: a description onto a column that does not exist, a
description asserting a different glossary term than the one the catalog
assigns, and a filler description that omits the column's expected concept
(plus, if the tag tool is healthy, a PII flag on a deliberately non-sensitive
column). Every call is observed (A1); afterwards we ground the observations
against the catalog and emit findings (A2).

The point: the findings cite specific catalog facts (a glossary term, a PII
classification, a schema), which a prompt/token tracer cannot produce. Exit 0
only if the rogue agent is flagged with those reasons and the good agent is
clean. Writes are reverted so the demo catalog stays clean.

Run on the box:  ~/fresh-e2e/v/bin/python scripts/ground_session.py
"""

from __future__ import annotations

import os
import sys
import tempfile

from heimdall.grounding import (
    CHECK_GLOSSARY_CONFLICT,
    CHECK_LOW_QUALITY,
    CHECK_PII_SCOPE,
    CHECK_UNDEFINED_COLUMN,
    SEV_HARMFUL,
    FindingStore,
    WorldCatalogContext,
    ground_events,
)
from heimdall.mcp_client import DataHubMCP
from heimdall.observability import EventStore
from heimdall.simulator.world import build_default_world

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable


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
            "HEIMDALL_AGENT_ID": agent_id,
            "HEIMDALL_EVENTS": events_db,
            "LEDGER_DB": ledger_db,
            "HEIMDALL_POLICY": "annotate",
            "MCP_SERVER_DATAHUB": mcp_server,
            "DATAHUB_GMS_URL": GMS,
        },
    )


def try_call(mcp, tool, args, label):
    try:
        mcp.call(tool, args)
        print(f"  ok    {label}")
    except RuntimeError as exc:
        # the observation is captured regardless of downstream outcome
        print(f"  sent  {label} (downstream: {str(exc)[:70]})")


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    tmp = tempfile.mkdtemp(prefix="heimdall-a2-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")

    world = build_default_world()
    orders = world.datasets["raw_orders"].urn
    payments = world.datasets["raw_payments"].urn

    print("phase 1: good-agent writes correct metadata")
    with client("good-agent", events_db, ledger_db, mcp_server) as mcp:
        try_call(mcp, "update_description", {
            "entity_urn": orders, "column_path": "order_id",
            "description": "Primary key of the order.", "operation": "replace",
        }, "describe order_id correctly")
        try_call(mcp, "update_description", {
            "entity_urn": payments, "column_path": "amount_usd",
            "description": "Settled payment amount in usd; the amount paid.",
            "operation": "replace",
        }, "describe amount_usd correctly")

    print("phase 2: rogue-agent writes catalog-violating metadata")
    with client("rogue-agent", events_db, ledger_db, mcp_server) as mcp:
        try_call(mcp, "update_description", {
            "entity_urn": orders, "column_path": "ghost_column",
            "description": "A column we invented.", "operation": "replace",
        }, "describe a column that does not exist")
        try_call(mcp, "update_description", {
            "entity_urn": payments, "column_path": "amount_usd",
            "description": "The gross order value in usd.", "operation": "replace",
        }, "describe amount_usd as the wrong glossary term")
        try_call(mcp, "update_description", {
            "entity_urn": orders, "column_path": "order_total_usd",
            "description": "This is a column.", "operation": "replace",
        }, "write a filler description")
        # PII false-positive: customer_id is a deliberate non-PII trap.
        try_call(mcp, "add_tags", {
            "entity_urns": [orders], "column_paths": ["customer_id"],
            "tag_urns": ["urn:li:tag:pii-email"],
        }, "flag a non-PII column as PII")

    # cleanup: revert the real description writes so the catalog stays clean
    print("cleanup: reverting writes")
    with client("cleanup", events_db, ledger_db, mcp_server) as mcp:
        for urn, col in [(orders, "order_id"), (payments, "amount_usd"),
                         (orders, "order_total_usd"), (orders, "ghost_column")]:
            try:
                mcp.call("update_description",
                         {"entity_urn": urn, "column_path": col, "operation": "remove"})
            except RuntimeError:
                pass
        try:
            mcp.call("remove_tags", {
                "entity_urns": [orders], "column_paths": ["customer_id"],
                "tag_urns": ["urn:li:tag:pii-email"],
            })
        except RuntimeError:
            pass

    # -- ground the captured observations (A2) --------------------------------
    events = EventStore(events_db).events()
    ctx = WorldCatalogContext(world)
    store = FindingStore(os.path.join(tmp, "findings.db"))
    findings = ground_events(events, ctx, store)

    print(f"\n=== grounded findings ({len(findings)}) ===")
    for f in findings:
        loc = f.column or "(table)"
        print(f"  [{f.severity:7}] {f.agent_id:12} {f.check_type:20} {loc}")
        print(f"            reason: {f.reason}")
    print(f"\nper-agent finding summary: {store.summary()}")

    # -- assertions -----------------------------------------------------------
    by_agent = {}
    for f in findings:
        by_agent.setdefault(f.agent_id, []).append(f)
    rogue = by_agent.get("rogue-agent", [])
    rogue_checks = {f.check_type for f in rogue}
    rogue_harmful = [f for f in rogue if f.severity == SEV_HARMFUL]
    glossary = [f for f in rogue if f.check_type == CHECK_GLOSSARY_CONFLICT]

    checks = [
        ("good agent has zero findings", not by_agent.get("good-agent")),
        ("rogue agent flagged", len(rogue) >= 3),
        ("undefined-column caught", CHECK_UNDEFINED_COLUMN in rogue_checks),
        ("glossary conflict caught", CHECK_GLOSSARY_CONFLICT in rogue_checks),
        ("low-quality caught", CHECK_LOW_QUALITY in rogue_checks),
        ("glossary reason cites both terms",
         bool(glossary) and "Gross Order Value" in glossary[0].reason
         and "Settled Payment Amount" in glossary[0].reason),
        ("harmful findings present", len(rogue_harmful) >= 2),
        ("only the rogue agent is flagged", set(by_agent) <= {"rogue-agent"}),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    # PII scope is a bonus: only assertable if the tag tool is healthy on the box
    if CHECK_PII_SCOPE in rogue_checks:
        print("  [bonus] PII scope violation also caught")

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} ({len(findings)} findings)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
