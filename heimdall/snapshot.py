"""Build the console's public projection from the observability stores.

One computation path feeds the public tables the console reads: hd_activity
(the live feed), hd_findings (grounded issues), hd_agents (the ranked
leaderboard). Rows are emitted as plain dicts so any transport (a service-key
PostgREST writer, or a jsonb_to_recordset bulk insert) consumes the same data
and the console can never drift from what the ledger and event store say.

owner is the tenant. The public showcase uses owner='showcase'; real per-user
data is scoped by owner under RLS once auth lands.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .claims import ClaimStore
from .grounding import FindingStore
from .observability import EventStore
from .trust import hd_agents_rows

SHOWCASE = "showcase"
CATALOG = "lineworld"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def activity_rows(
    event_store: EventStore, owner: str = SHOWCASE, catalog: str = CATALOG,
    limit: int = 150, since_ts: float | None = None,
) -> list[dict[str, Any]]:
    events = event_store.events()
    if since_ts is not None:
        events = [e for e in events if e.ts >= since_ts]
    events = events[-limit:]  # most recent
    return [
        {
            "agent_id": e.agent_id, "tool": e.tool, "op": e.op, "status": e.status,
            "args": e.args, "entities": e.entities, "latency_ms": e.latency_ms,
            "result_summary": e.result_summary, "ts": _iso(e.ts),
            "owner": owner, "catalog": catalog,
        }
        for e in events
    ]


def findings_rows(
    finding_store: FindingStore, owner: str = SHOWCASE, catalog: str = CATALOG,
    since_ts: float | None = None,
) -> list[dict[str, Any]]:
    findings = finding_store.findings()
    if since_ts is not None:
        findings = [f for f in findings if f.ts >= since_ts]
    return [
        {
            "agent_id": f.agent_id, "check_type": f.check_type, "severity": f.severity,
            "verdict": f.verdict, "entity_urn": f.entity_urn, "column": f.column,
            "reason": f.reason, "ts": _iso(f.ts), "owner": owner, "catalog": catalog,
        }
        for f in findings
    ]


def agents_rows(
    trust_store: ClaimStore, registry: dict[str, dict[str, Any]] | None = None,
    catalog: str = CATALOG, **kwargs: Any,
) -> list[dict[str, Any]]:
    rows = hd_agents_rows(trust_store, registry=registry, **kwargs)
    for r in rows:
        r["catalog"] = catalog
    return rows
