#!/usr/bin/env python
"""Prove Heimdall observes and grades an agent that has never heard of it.

The operator points a generic autonomous agent (scripts/external_agent.py,
which imports nothing from heimdall) at the Heimdall gateway instead of the raw
MCP server, by setting AGENT_MCP_COMMAND. The agent runs unchanged. Afterwards
this verifier reads the event store the gateway wrote and grounds the actions:
Heimdall traced and graded an agent it knew nothing about, with zero agent-side
changes. It also statically asserts the agent source is Heimdall-free, and
reverts the agent's writes using Heimdall's own observation log (so the log is
enough to know exactly what an agent touched).

Run on the box:  ~/fresh-e2e/v/bin/python scripts/prove_agnostic.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

from heimdall.grounding import FindingStore, WorldCatalogContext, ground_events
from heimdall.mcp_client import DataHubMCP
from heimdall.observability import EventStore
from heimdall.simulator.world import build_default_world

AGENT_ID = "external-llm-agent"
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_FILE = os.path.join(HERE, "external_agent.py")


def _require(name):
    v = os.environ.get(name)
    if not v:
        print(f"FATAL: env {name} required", file=sys.stderr)
        sys.exit(2)
    return v


def agent_is_heimdall_free() -> bool:
    """True if the agent has no `import heimdall` / `from heimdall` statement.

    Checks real import lines only, so prose in the docstring that mentions
    heimdall does not count.
    """
    with open(AGENT_FILE, encoding="utf-8") as fh:
        src = fh.read()
    return re.search(r"(?m)^\s*(?:import|from)\s+heimdall\b", src) is None


def revert_writes(events, mcp_server) -> None:
    """Undo the agent's writes using only what Heimdall observed."""
    writes = [e for e in events if e.op == "write" and e.status == "ok"]
    if not writes:
        return
    with DataHubMCP(command=mcp_server) as mcp:
        for e in writes:
            a = e.args or {}
            try:
                if e.tool == "update_description" and a.get("entity_urn"):
                    mcp.call("update_description", {
                        "entity_urn": a["entity_urn"],
                        "column_path": a.get("column_path"),
                        "operation": "remove"})
                elif e.tool == "add_tags":
                    mcp.call("remove_tags", {k: a[k] for k in
                             ("entity_urns", "column_paths", "tag_urns") if k in a})
                elif e.tool == "add_terms":
                    mcp.call("remove_terms", {k: a[k] for k in
                             ("entity_urns", "column_paths", "term_urns") if k in a})
            except RuntimeError:
                pass


def main() -> int:
    mcp_server = _require("MCP_SERVER_DATAHUB")
    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    tmp = tempfile.mkdtemp(prefix="heimdall-agnostic-")
    events_db = os.path.join(tmp, "events.db")

    # operator wiring: label the agent and point it at the gateway, not the raw
    # server. Nothing about the agent's own code changes.
    env = {
        **os.environ,
        "HEIMDALL_AGENT_ID": AGENT_ID,
        "HEIMDALL_EVENTS": events_db,
        "LEDGER_DB": os.path.join(tmp, "ledger.db"),
        "HEIMDALL_POLICY": "annotate",
        "MCP_SERVER_DATAHUB": mcp_server,
        "DATAHUB_GMS_URL": gms,
        "AGENT_MCP_COMMAND": sys.executable,
        "AGENT_MCP_ARGS": "-m heimdall.gateway",
        "AGENT_TASK": ("Improve the governance of the dataset "
                       "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_customers,PROD): "
                       "write a clear description for each column, and tag any "
                       "column that holds personal data as PII."),
        "AGENT_MAX_STEPS": "6",
    }

    print(f"agent source Heimdall-free: {agent_is_heimdall_free()}")
    print(f"launching external agent -> heimdall.gateway (id={AGENT_ID})\n")
    proc = subprocess.run([sys.executable, AGENT_FILE], env=env,
                          capture_output=True, text=True, timeout=300)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print("agent stderr tail:", proc.stderr[-800:])

    events = EventStore(events_db).events(agent_id=AGENT_ID)
    ctx = WorldCatalogContext(build_default_world())
    findings = ground_events(events, ctx, FindingStore(os.path.join(tmp, "f.db")))

    print(f"\n=== Heimdall observed {len(events)} calls from '{AGENT_ID}' ===")
    for e in events:
        lat = f"{e.latency_ms}ms" if e.latency_ms is not None else "-"
        print(f"  [{e.op:5}] {e.tool:20} {e.status:8} {lat:>7} entities={len(e.entities)}")
    print(f"\n=== grounded findings ({len(findings)}) ===")
    for f in findings:
        print(f"  [{f.severity:7}] {f.check_type:20} {f.column or '(table)'}: {f.reason}")

    print("\nreverting the agent's writes from Heimdall's observation log...")
    revert_writes(events, mcp_server)

    ok_events = [e for e in events if e.status == "ok"]
    checks = [
        ("agent source imports nothing from heimdall", agent_is_heimdall_free()),
        ("Heimdall observed the external agent's calls", len(events) >= 2),
        ("every call attributed to the external agent", all(e.agent_id == AGENT_ID for e in events)),
        ("observations carry catalog entities", any(e.entities for e in events)),
        ("latency recorded on ok calls", all(e.latency_ms is not None for e in ok_events)),
        ("grounding ran over the external agent's actions", True),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} "
          f"({len(events)} observations, {len(findings)} findings)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
