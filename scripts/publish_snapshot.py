#!/usr/bin/env python
"""Run a rich observability session on the box and emit the console snapshot.

Produces varied real activity: two agents documenting through the gateway in
annotate mode (an expert who is correct and a rogue who violates the catalog),
plus one agent under enforce mode whose writes get passed, held, and blocked.
Then grounds and scores everything and prints three JSON payloads
(ACTIVITY_JSON / FINDINGS_JSON / AGENTS_JSON) for the operator to load into the
public hd_* tables the console reads.

Run on the box:  ~/fresh-e2e/v/bin/python scripts/publish_snapshot.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from heimdall.claims import ClaimStore
from heimdall.grounding import FindingStore, WorldCatalogContext, ground_events
from heimdall.mcp_client import DataHubMCP
from heimdall.observability import EventStore
from heimdall.simulator.world import build_default_world
from heimdall.snapshot import activity_rows, agents_rows, findings_rows
from heimdall.trust import settle_observations

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable

CORRECT = [
    ("raw_orders", "order_total_usd", "Total order amount in usd."),
    ("raw_orders", "discount_code", "Promotional discount coupon code."),
    ("raw_payments", "amount_usd", "Amount paid in usd, once settled."),
    ("stg_payments", "amount_usd", "Amount paid in usd, staged."),
    ("fct_orders", "discount_usd", "Discount amount in usd."),
    ("raw_customers", "email", "Customer email address."),
]
VIOLATIONS = [  # (dataset, col, desc) filler / wrong-glossary
    ("raw_orders", "order_total_usd", "a column"),
    ("raw_payments", "amount_usd", "The gross order value in usd."),   # glossary conflict
    ("raw_customers", "country_code", "a column"),
    ("fct_revenue", "paid_usd", "a column"),
    ("fct_engagement", "events_30d", "a column"),
    ("raw_web_events", "event_type", "a column"),
]


def _require(name):
    v = os.environ.get(name)
    if not v:
        print(f"FATAL: env {name} required", file=sys.stderr)
        sys.exit(2)
    return v


def client(agent_id, events_db, ledger_db, mcp_server, policy="annotate"):
    return DataHubMCP(gms_url=GMS, command=PYEXE, args=["-m", "heimdall.gateway"],
                      extra_env={"HEIMDALL_AGENT_ID": agent_id, "HEIMDALL_EVENTS": events_db,
                                 "LEDGER_DB": ledger_db, "HEIMDALL_POLICY": policy,
                                 "HEIMDALL_CATALOG": "world",
                                 "MCP_SERVER_DATAHUB": mcp_server, "DATAHUB_GMS_URL": GMS})


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    tmp = tempfile.mkdtemp(prefix="heimdall-snap-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")
    world = build_default_world()

    def urn(ds):
        return world.datasets[ds].urn

    # expert-doc: correct documentation + a couple of reads
    with client("expert-doc", events_db, ledger_db, mcp_server) as mcp:
        for ds, col, desc in CORRECT:
            try:
                mcp.get_entities([urn(ds)])
            except Exception:
                pass
            try:
                mcp.call("update_description", {"entity_urn": urn(ds), "column_path": col,
                         "description": desc, "operation": "replace"})
            except Exception:
                pass

    # rogue-doc: catalog violations
    with client("rogue-doc", events_db, ledger_db, mcp_server) as mcp:
        for ds, col, desc in VIOLATIONS:
            try:
                mcp.call("update_description", {"entity_urn": urn(ds), "column_path": col,
                         "description": desc, "operation": "replace"})
            except Exception:
                pass
        # a PII mis-tag on a non-PII column
        try:
            mcp.call("add_tags", {"entity_urns": [urn("raw_orders")],
                     "column_paths": ["customer_id"], "tag_urns": ["urn:li:tag:pii-email"]})
        except Exception:
            pass

    # guarded-agent under enforce: pass, hold, block
    with client("guarded-agent", events_db, ledger_db, mcp_server, policy="enforce") as mcp:
        for col, desc in [("order_total_usd", "Total order amount in usd."),  # pass
                          ("discount_code", "a column"),                       # hold
                          ]:
            try:
                mcp.call("update_description", {"entity_urn": urn("raw_orders"),
                         "column_path": col, "description": desc, "operation": "replace"})
            except Exception:
                pass
        try:  # block: glossary conflict
            mcp.call("update_description", {"entity_urn": urn("raw_payments"),
                     "column_path": "amount_usd", "description": "The gross order value in usd.",
                     "operation": "replace"})
        except Exception:
            pass

    # cleanup the writes that landed (keep the catalog tidy)
    with client("cleanup", events_db, ledger_db, mcp_server) as mcp:
        for ds, col, _ in CORRECT + VIOLATIONS:
            try:
                mcp.call("update_description", {"entity_urn": urn(ds), "column_path": col,
                         "operation": "remove"})
            except Exception:
                pass
        try:
            mcp.call("remove_tags", {"entity_urns": [urn("raw_orders")],
                     "column_paths": ["customer_id"], "tag_urns": ["urn:li:tag:pii-email"]})
        except Exception:
            pass

    # ground + settle
    events = EventStore(events_db).events()
    ctx = WorldCatalogContext(world)
    finding_store = FindingStore(os.path.join(tmp, "findings.db"))
    ground_events(events, ctx, finding_store)
    trust_store = ClaimStore(os.path.join(tmp, "trust.db"))
    settle_observations(events, ctx, trust_store)

    activity = activity_rows(EventStore(events_db))
    findings = findings_rows(finding_store)
    agents = agents_rows(trust_store)

    sys.stderr.write(f"events={len(activity)} findings={len(findings)} agents={len(agents)}\n")
    print("ACTIVITY_JSON=" + json.dumps(activity))
    print("FINDINGS_JSON=" + json.dumps(findings))
    print("AGENTS_JSON=" + json.dumps(agents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
