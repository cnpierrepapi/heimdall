"""Observation event store: the live record of what agents did.

Every tool call an agent makes through the gateway, read or write, becomes
one ObservationEvent: who called what, against which catalog entities, how
long it took, and how it ended (ok, error, or blocked by policy). This is the
raw material the grounded evaluators (findings) and the trust engine consume.

An observation is a superset of a claim. A claim says "agent A asserts this
description is correct" and is settleable against ground truth. An observation
just says "agent A called update_description on this column at this time" and
covers reads too, which never become claims but are most of what an agent does
and where catalog grounding has the most to check. So the two live in separate
tables, both SQLite for zero-service portability, both WAL for concurrent read.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import BaseModel, Field

# op values
READ = "read"
WRITE = "write"
# status values
OK = "ok"
ERROR = "error"
BLOCKED = "blocked"
HELD = "held"

_URN_START_RE = re.compile(r"urn:li:[a-zA-Z]+:")
# characters that cannot appear inside a simple (non-parenthesised) urn value
_URN_STOP = set('"\',} \t\r\n]')

_MAX_ENTITIES = 32
_MAX_ARG_STR = 500
_MAX_SUMMARY = 240


def extract_entity_urns(payload: Any, limit: int = _MAX_ENTITIES) -> list[str]:
    """Every catalog entity urn referenced in a payload, in discovery order.

    Handles both simple urns (urn:li:tag:pii) and parenthesised urns with
    nested commas and parens (urn:li:schemaField:(urn:li:dataset:(...),col))
    by tracking paren depth rather than splitting naively. Deduped, capped.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload)
    seen: set[str] = set()
    out: list[str] = []
    for m in _URN_START_RE.finditer(text):
        i = m.end()
        if i < len(text) and text[i] == "(":
            depth = 0
            j = i
            while j < len(text):
                c = text[j]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            urn = text[m.start():j]
        else:
            j = i
            while j < len(text) and text[j] not in _URN_STOP:
                j += 1
            urn = text[m.start():j]
        if not urn or urn in seen:
            continue
        seen.add(urn)
        out.append(urn)
        if len(out) >= limit:
            break
    return out


def sanitize_args(args: Any, max_str: int = _MAX_ARG_STR) -> Any:
    """A JSON-safe, size-bounded copy of tool arguments for storage.

    Long string values (a whole generated description, say) are truncated so
    the event log stays small; structure is preserved so it stays queryable.
    """
    if isinstance(args, str):
        return args if len(args) <= max_str else args[:max_str] + "...(truncated)"
    if isinstance(args, dict):
        return {str(k): sanitize_args(v, max_str) for k, v in args.items()}
    if isinstance(args, (list, tuple)):
        return [sanitize_args(v, max_str) for v in args]
    if isinstance(args, (int, float, bool)) or args is None:
        return args
    return sanitize_args(str(args), max_str)


class ObservationEvent(BaseModel):
    """One tool call by one agent through the gateway."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    tool: str
    op: str                       # read | write
    status: str = OK              # ok | error | blocked
    args: dict[str, Any] = Field(default_factory=dict)
    entities: list[str] = Field(default_factory=list)
    latency_ms: Optional[int] = None
    result_summary: str = ""
    error: Optional[str] = None
    ts: float = Field(default_factory=time.time)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    event_id       TEXT PRIMARY KEY,
    ts             REAL NOT NULL,
    agent_id       TEXT NOT NULL,
    tool           TEXT NOT NULL,
    op             TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'ok',
    args           TEXT NOT NULL DEFAULT '{}',
    entities       TEXT NOT NULL DEFAULT '[]',
    latency_ms     INTEGER,
    result_summary TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_agent ON observations (agent_id, ts);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations (ts);
"""


class EventStore:
    """SQLite store of observation events. One writer, many readers (WAL)."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def record(self, event: ObservationEvent) -> ObservationEvent:
        self._conn.execute(
            "INSERT INTO observations (event_id, ts, agent_id, tool, op, status,"
            " args, entities, latency_ms, result_summary, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.ts,
                event.agent_id,
                event.tool,
                event.op,
                event.status,
                json.dumps(event.args),
                json.dumps(event.entities),
                event.latency_ms,
                event.result_summary,
                event.error,
            ),
        )
        self._conn.commit()
        return event

    # -- reads -------------------------------------------------------------

    def events(
        self,
        agent_id: Optional[str] = None,
        op: Optional[str] = None,
        status: Optional[str] = None,
        entity_urn: Optional[str] = None,
        since_ts: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[ObservationEvent]:
        sql = "SELECT * FROM observations WHERE 1=1"
        params: list[Any] = []
        if agent_id is not None:
            sql += " AND agent_id=?"
            params.append(agent_id)
        if op is not None:
            sql += " AND op=?"
            params.append(op)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        if entity_urn is not None:
            sql += " AND entities LIKE ?"
            params.append(f"%{entity_urn}%")
        if since_ts is not None:
            sql += " AND ts >= ?"
            params.append(since_ts)
        sql += " ORDER BY ts"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def agent_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT agent_id FROM observations ORDER BY agent_id"
        ).fetchall()
        return [r["agent_id"] for r in rows]

    def summary(self) -> dict[str, dict[str, int]]:
        """Per-agent activity counts: total, reads, writes, errors, blocked."""
        rows = self._conn.execute(
            "SELECT agent_id,"
            " COUNT(*) AS total,"
            " SUM(CASE WHEN op='read' THEN 1 ELSE 0 END) AS reads,"
            " SUM(CASE WHEN op='write' THEN 1 ELSE 0 END) AS writes,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,"
            " SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked,"
            " SUM(CASE WHEN status='held' THEN 1 ELSE 0 END) AS held"
            " FROM observations GROUP BY agent_id"
        ).fetchall()
        return {
            r["agent_id"]: {
                "total": r["total"],
                "reads": r["reads"] or 0,
                "writes": r["writes"] or 0,
                "errors": r["errors"] or 0,
                "blocked": r["blocked"] or 0,
                "held": r["held"] or 0,
            }
            for r in rows
        }

    def __iter__(self) -> Iterator[ObservationEvent]:
        return iter(self.events())


def _row_to_event(row: sqlite3.Row) -> ObservationEvent:
    return ObservationEvent(
        event_id=row["event_id"],
        ts=row["ts"],
        agent_id=row["agent_id"],
        tool=row["tool"],
        op=row["op"],
        status=row["status"],
        args=json.loads(row["args"]),
        entities=json.loads(row["entities"]),
        latency_ms=row["latency_ms"],
        result_summary=row["result_summary"] or "",
        error=row["error"],
    )


def summarize_result(text: str, limit: int = _MAX_SUMMARY) -> str:
    """A compact one-line-ish summary of a tool result for the event log."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."
