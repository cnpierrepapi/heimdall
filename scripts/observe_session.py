#!/usr/bin/env python
"""A1 live proof: a real agent session, traced end to end by the gateway.

An agent connects to the heimdall gateway (not to mcp-server-datahub directly)
and does real catalog work against the live DataHub on the box: it reads a
dataset's schema, entities, and lineage, then writes a column description and
reads it back, then cleans up. A second, low-trust agent tries to write under
an enforce policy and is blocked. Every one of those calls flows

    agent -> heimdall.gateway -> mcp-server-datahub -> GMS

so the gateway observes the whole session. Afterwards we open the event store
the gateway wrote and print the captured trace. Exit code is 0 only if the
trace is complete (reads, a write, entities, latency, and the blocked attempt
all captured). Search is avoided (G-30): datasets come from the world model.

Env required: MCP_SERVER_DATAHUB (downstream server), DATAHUB_GMS_URL.
Run on the box:  ~/fresh-e2e/v/bin/python scripts/observe_session.py
"""

from __future__ import annotations

import os
import sys
import tempfile

from heimdall.mcp_client import DataHubMCP
from heimdall.observability import BLOCKED, ERROR, OK, READ, WRITE, EventStore
from heimdall.simulator.world import build_default_world

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"FATAL: env {name} is required", file=sys.stderr)
        sys.exit(2)
    return val


def gateway_client(agent_id, events_db, ledger_db, mcp_server,
                   policy="annotate", min_trust="0") -> DataHubMCP:
    """A DataHubMCP session pointed at the heimdall gateway, not the raw server."""
    return DataHubMCP(
        gms_url=GMS,
        command=PYEXE,
        args=["-m", "heimdall.gateway"],
        extra_env={
            "HEIMDALL_AGENT_ID": agent_id,
            "HEIMDALL_EVENTS": events_db,
            "LEDGER_DB": ledger_db,
            "HEIMDALL_POLICY": policy,
            "HEIMDALL_MIN_TRUST": min_trust,
            "MCP_SERVER_DATAHUB": mcp_server,
            "DATAHUB_GMS_URL": GMS,
        },
    )


def short(urn: str, n: int = 48) -> str:
    return urn if len(urn) <= n else urn[:n] + "..."


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    tmp = tempfile.mkdtemp(prefix="heimdall-a1-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")

    world = build_default_world()
    dataset = world.datasets["raw_orders"]
    dataset_urn = dataset.urn
    column = dataset.columns[0].name
    print(f"target dataset: {dataset_urn}")
    print(f"target column:  {column}")
    print(f"event store:    {events_db}\n")

    # -- Phase 1: a competent agent reads, then makes a real write ------------
    # The gateway observes every call whether it succeeds or fails downstream,
    # so the session is driven tolerantly: a failing read is a captured error,
    # not a reason to stop. (On this box, some reads hit a degraded search
    # index; that is exactly the kind of thing observability should surface.)
    print("phase 1: observed-agent works through the gateway")

    def step(mcp, tool, args, label):
        try:
            mcp.call(tool, args)
            print(f"  ok    {label}")
        except RuntimeError as exc:
            print(f"  error {label}: {str(exc)[:90]}")

    with gateway_client("observed-agent", events_db, ledger_db, mcp_server) as mcp:
        step(mcp, "list_schema_fields", {"urn": dataset_urn}, "read schema")
        step(mcp, "get_lineage", {"urn": dataset_urn, "upstream": True, "max_hops": 2}, "read lineage")
        step(mcp, "get_entities", {"urns": [dataset_urn]}, "read entity")
        step(mcp, "update_description", {                          # write (replace)
            "entity_urn": dataset_urn,
            "column_path": column,
            "description": "Observed by Heimdall A1 proof.",
            "operation": "replace",
        }, "write description")
        step(mcp, "get_entities", {"urns": [dataset_urn]}, "read back")
        step(mcp, "update_description", {                          # write (cleanup)
            "entity_urn": dataset_urn,
            "column_path": column,
            "operation": "remove",
        }, "revert description")

    # -- Phase 2: a low-trust agent is blocked, and that too is observed ------
    print("phase 2: rogue-agent write under enforce policy (min_trust 99)")
    blocked_raised = False
    with gateway_client("rogue-agent", events_db, ledger_db, mcp_server,
                        policy="enforce", min_trust="99") as mcp2:
        try:
            mcp2.call("update_description", {
                "entity_urn": dataset_urn,
                "column_path": column,
                "description": "should never land",
                "operation": "replace",
            })
        except RuntimeError as exc:
            blocked_raised = "policy" in str(exc).lower()
            print(f"  blocked: {exc}")

    # -- inspect the captured trace ------------------------------------------
    store = EventStore(events_db)
    events = store.events()
    print("\n=== captured observation trace ===")
    for e in events:
        ent = short(e.entities[0]) if e.entities else "-"
        lat = f"{e.latency_ms}ms" if e.latency_ms is not None else "-"
        print(f"  [{e.op:5}] {e.tool:20} {e.status:8} {lat:>7}  "
              f"agent={e.agent_id:14} entities={len(e.entities)} {ent}")
    print(f"\nper-agent summary: {store.summary()}")

    # -- assertions ----------------------------------------------------------
    reads_ok = [e for e in events if e.op == READ and e.status == OK]
    reads_err = [e for e in events if e.op == READ and e.status == ERROR]
    writes = [e for e in events if e.op == WRITE and e.status == OK]
    blocked = [e for e in events if e.status == BLOCKED]
    ok_events = [e for e in events if e.status == OK]

    checks = [
        ("reads observed", len(reads_ok) >= 1),
        ("real write observed and succeeded", len(writes) >= 1),
        ("failed downstream calls observed as errors", len(reads_err) >= 1),
        ("errors carry a message", all(e.error for e in reads_err)),
        ("target dataset captured as an entity touched",
         any(dataset_urn in e.entities for e in events)),
        ("latency recorded on every ok event", all(e.latency_ms is not None for e in ok_events)),
        ("args captured on writes", all(e.args for e in writes)),
        ("blocked write observed", len(blocked) >= 1),
        ("enforce actually raised to the agent", blocked_raised),
        ("both agents observed", set(store.agent_ids()) == {"observed-agent", "rogue-agent"}),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} ({len(events)} events captured)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
