#!/usr/bin/env python
"""C2 live proof: write the trust verdict back into DataHub as an audit trail.

Two agents document disjoint datasets through the gateway: an expert (correct
descriptions) and a rogue (filler). Heimdall observes and grounds every write,
settles them into trust scores, then projects the verdict back into the catalog:
each dataset an agent touched is tagged and stamped with the author agent, its
trust score, and its skill-vs-luck verdict, and a per-agent dossier is saved as
a Document. We then read the stamps back from DataHub (via the SDK graph, which
is reliable even while GraphQL search is degraded on this box) to prove the
audit trail actually landed where a steward would see it.

Run on the box:  ~/fresh-e2e/v/bin/python scripts/audit_session.py
"""

from __future__ import annotations

import os
import sys
import tempfile

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import GlobalTagsClass, StructuredPropertiesClass

from heimdall.audit import audit_writeback
from heimdall.grounding import FindingStore, WorldCatalogContext, ground_events
from heimdall.mcp_client import DataHubMCP
from heimdall.observability import EventStore
from heimdall.simulator.world import build_default_world
from heimdall.trust import settle_observations
from heimdall.writeback import PROP_AGENT, PROP_VERDICT, TAG_HARMFUL, TAG_SKILLED, tag_urn
from heimdall.claims import ClaimStore

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
PYEXE = sys.executable

EXPERT = [
    ("raw_orders", "order_total_usd", "Total order amount in usd."),
    ("raw_orders", "discount_code", "Promotional discount coupon code."),
    ("raw_payments", "amount_usd", "Amount paid in usd, once settled."),
    ("stg_payments", "amount_usd", "Amount paid in usd, staged."),
    ("fct_orders", "discount_usd", "Discount amount in usd."),
]
ROGUE = [
    ("raw_customers", "email", "a column here"),
    ("raw_customers", "country_code", "a column here"),
    ("raw_web_events", "event_type", "a column here"),
    ("fct_revenue", "paid_usd", "a column here"),
    ("fct_engagement", "events_30d", "a column here"),
]


def _require(name):
    v = os.environ.get(name)
    if not v:
        print(f"FATAL: env {name} required", file=sys.stderr)
        sys.exit(2)
    return v


def client(agent_id, events_db, ledger_db, mcp_server):
    return DataHubMCP(gms_url=GMS, command=PYEXE, args=["-m", "heimdall.gateway"],
                      extra_env={"HEIMDALL_AGENT_ID": agent_id, "HEIMDALL_EVENTS": events_db,
                                 "LEDGER_DB": ledger_db, "HEIMDALL_POLICY": "annotate",
                                 "MCP_SERVER_DATAHUB": mcp_server, "DATAHUB_GMS_URL": GMS})


def document(agent, rows, world, events_db, ledger_db, mcp_server):
    with client(agent, events_db, ledger_db, mcp_server) as mcp:
        for ds, col, desc in rows:
            try:
                mcp.call("update_description", {"entity_urn": world.datasets[ds].urn,
                         "column_path": col, "description": desc, "operation": "replace"})
            except RuntimeError as exc:
                print(f"  ({agent} write to {ds}.{col} downstream: {str(exc)[:50]})")


def prop_values(graph, urn):
    aspect = graph.get_aspect(urn, StructuredPropertiesClass)
    if not aspect:
        return {}
    return {a.propertyUrn: a.values[0] for a in aspect.properties if a.values}


def tag_names(graph, urn):
    aspect = graph.get_aspect(urn, GlobalTagsClass)
    return [t.tag for t in aspect.tags] if aspect else []


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    tmp = tempfile.mkdtemp(prefix="heimdall-c2-")
    events_db = os.path.join(tmp, "events.db")
    ledger_db = os.path.join(tmp, "ledger.db")
    world = build_default_world()

    print("expert-doc and rogue-doc document disjoint datasets through the gateway")
    document("expert-doc", EXPERT, world, events_db, ledger_db, mcp_server)
    document("rogue-doc", ROGUE, world, events_db, ledger_db, mcp_server)

    # observe -> ground -> settle -> score
    events = EventStore(events_db).events()
    ctx = WorldCatalogContext(world)
    finding_store = FindingStore(os.path.join(tmp, "findings.db"))
    ground_events(events, ctx, finding_store)
    trust_store = ClaimStore(os.path.join(tmp, "trust.db"))
    settle_observations(events, ctx, trust_store)

    # project the verdict back into DataHub
    print("writing the audit trail back into DataHub...")
    with DataHubMCP(gms_url=GMS, command=mcp_server) as mcp:
        result = audit_writeback(mcp, EventStore(events_db), finding_store, trust_store,
                                 gms_url=GMS)
    print(f"datasets stamped: {len(result['datasets_stamped'])}, "
          f"dossiers: {result['dossiers_published']}")

    # read the stamps back from DataHub via the SDK graph (reliable on this box)
    graph = DataHubGraph(DatahubClientConfig(server=GMS))
    orders = world.datasets["raw_orders"].urn
    customers = world.datasets["raw_customers"].urn
    o_props, o_tags = prop_values(graph, orders), tag_names(graph, orders)
    c_props, c_tags = prop_values(graph, customers), tag_names(graph, customers)

    print("\n=== provenance read back from DataHub ===")
    print(f"  raw_orders   author={o_props.get(PROP_AGENT)} verdict={o_props.get(PROP_VERDICT)} tags={o_tags}")
    print(f"  raw_customers author={c_props.get(PROP_AGENT)} verdict={c_props.get(PROP_VERDICT)} tags={c_tags}")

    checks = [
        ("expert dataset stamped with expert-doc", o_props.get(PROP_AGENT) == "expert-doc"),
        ("expert dataset carries skilled tag", tag_urn(TAG_SKILLED) in o_tags),
        ("rogue dataset stamped with rogue-doc", c_props.get(PROP_AGENT) == "rogue-doc"),
        ("rogue dataset carries harmful verdict", c_props.get(PROP_VERDICT) == "worse than chance"),
        ("rogue dataset carries harmful tag", tag_urn(TAG_HARMFUL) in c_tags),
        ("both dossiers published", len(result["dossiers_published"]) == 2),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    # tidy: remove the filler descriptions the rogue wrote (keep the stamps)
    with DataHubMCP(gms_url=GMS, command=mcp_server) as mcp:
        for ds, col, _ in ROGUE:
            try:
                mcp.call("update_description", {"entity_urn": world.datasets[ds].urn,
                         "column_path": col, "operation": "remove"})
            except RuntimeError:
                pass

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
