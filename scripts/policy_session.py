#!/usr/bin/env python
"""C1 live proof: the gateway as a control plane, deciding writes in flight.

One agent makes three writes through the gateway in enforce mode against the
live DataHub on the box. Heimdall grounds each write against the catalog before
forwarding it and decides:

  clean description        -> pass, applied to the catalog
  filler description       -> held for review, NOT applied
  glossary-contradiction   -> blocked, NOT applied (the agent gets the reason)

The point: a catalog-violating write is stopped before it lands, with a
specific reason, not caught after the fact. Exit 0 only if the clean write is
the only one that reached the catalog. The clean write is reverted afterward.

Run on the box:  ~/fresh-e2e/v/bin/python scripts/policy_session.py
"""

from __future__ import annotations

import os
import sys
import tempfile

from heimdall.mcp_client import DataHubMCP
from heimdall.observability import BLOCKED, HELD, OK, EventStore
from heimdall.simulator.world import build_default_world

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable


def _require(name):
    v = os.environ.get(name)
    if not v:
        print(f"FATAL: env {name} required", file=sys.stderr)
        sys.exit(2)
    return v


def enforce_client(events_db, ledger_db, mcp_server):
    return DataHubMCP(
        gms_url=GMS, command=PYEXE, args=["-m", "heimdall.gateway"],
        extra_env={
            "HEIMDALL_AGENT_ID": "writer", "HEIMDALL_EVENTS": events_db,
            "LEDGER_DB": ledger_db, "HEIMDALL_POLICY": "enforce",
            "HEIMDALL_CATALOG": "world", "HEIMDALL_MIN_TRUST": "0",
            "MCP_SERVER_DATAHUB": mcp_server, "DATAHUB_GMS_URL": GMS,
        },
    )


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    tmp = tempfile.mkdtemp(prefix="heimdall-c1-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")
    world = build_default_world()
    orders = world.datasets["raw_orders"].urn
    payments = world.datasets["raw_payments"].urn

    passed = held = blocked = False

    with enforce_client(events_db, ledger_db, mcp_server) as mcp:
        # 1. a clean, correct description: should pass and apply
        try:
            mcp.call("update_description", {
                "entity_urn": orders, "column_path": "order_total_usd",
                "description": "Total order amount in usd.", "operation": "replace"})
            passed = True
            print("clean write: PASSED (applied)")
        except RuntimeError as exc:
            print(f"clean write unexpectedly failed: {str(exc)[:80]}")

        # 2. a filler description: should be held, not applied
        try:
            mcp.call("update_description", {
                "entity_urn": orders, "column_path": "order_total_usd",
                "description": "a column", "operation": "replace"})
            print("filler write: NOT HELD (unexpected)")
        except RuntimeError as exc:
            held = "held for review" in str(exc)
            print(f"filler write: {'HELD (not applied)' if held else 'unexpected: ' + str(exc)[:80]}")

        # 3. a glossary-contradicting description: should be blocked, not applied
        try:
            mcp.call("update_description", {
                "entity_urn": payments, "column_path": "amount_usd",
                "description": "The gross order value in usd.", "operation": "replace"})
            print("conflict write: NOT BLOCKED (unexpected)")
        except RuntimeError as exc:
            blocked = "glossary term" in str(exc)
            print(f"conflict write: BLOCKED -> {str(exc).split('failed:')[-1].strip()[:120]}")

    # cleanup: only the clean write applied; revert it
    with enforce_client(events_db, ledger_db, mcp_server) as mcp:
        try:
            mcp.call("update_description", {"entity_urn": orders,
                     "column_path": "order_total_usd", "operation": "remove"})
        except RuntimeError:
            pass

    events = EventStore(events_db).events(agent_id="writer")
    ok_writes = [e for e in events if e.op == "write" and e.status == OK]
    held_writes = [e for e in events if e.status == HELD]
    blocked_writes = [e for e in events if e.status == BLOCKED]

    print("\n=== observed write decisions ===")
    for e in events:
        if e.op == "write":
            print(f"  {e.tool:20} {e.status:8} {e.error or ''}"[:110])

    # note: the cleanup remove also applies; count only the graded writes above
    graded_ok = [e for e in ok_writes if e.args.get("operation") != "remove"]
    checks = [
        ("clean write passed and applied", passed and len(graded_ok) >= 1),
        ("filler write was held", held and len(held_writes) >= 1),
        ("conflict write was blocked with a catalog reason", blocked and len(blocked_writes) >= 1),
        ("only the clean write reached the catalog", len(graded_ok) == 1),
    ]
    print("\n=== checks ===")
    ok = True
    for label, ok_ in checks:
        print(f"  [{'PASS' if ok_ else 'FAIL'}] {label}")
        ok = ok and ok_
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
