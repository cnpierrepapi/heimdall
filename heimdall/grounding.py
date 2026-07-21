"""Catalog-grounded evaluators: turn an observed action into findings.

This is the differentiator. A generic LLM tracer watches prompts, tokens, and
latency; it has no model of your data, so it cannot tell you that an agent just
wrote a description contradicting the glossary, tagged a non-sensitive column as
PII, or documented a column that does not exist. Heimdall observes from inside
the DataHub path, so every finding is grounded in the catalog the agent acted
on and cites the specific catalog fact it violated.

Each evaluator is a pure function of (parsed action, catalog context). The
context is an abstraction: WorldCatalogContext backs it with the demo world's
known truth; a DataHubCatalogContext reading live glossary/schema/PII/ownership
is the production backing (same evaluators, different source). Findings are
recorded in their own SQLite store so the console and the trust engine can read
them.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

from pydantic import BaseModel, Field

from .observability import ObservationEvent

# -- check types and severities -----------------------------------------------

CHECK_UNDEFINED_COLUMN = "undefined_column"
CHECK_GLOSSARY_CONFLICT = "glossary_conflict"
CHECK_LOW_QUALITY = "low_quality_description"
CHECK_PII_SCOPE = "pii_scope"
CHECK_WRONG_OWNER = "wrong_owner"
CHECK_WRONG_DOMAIN = "wrong_domain"

SEV_INFO = "info"
SEV_WARN = "warn"
SEV_HARMFUL = "harmful"

FAIL = "fail"


# -- catalog context ----------------------------------------------------------


class CatalogContext(Protocol):
    """What the evaluators need to know about the catalog to ground an action."""

    def dataset_name(self, urn: str) -> Optional[str]: ...
    def columns(self, dataset: str) -> list[str]: ...
    def column_pii(self, dataset: str, column: str) -> Optional[str]: ...
    def column_term(self, dataset: str, column: str) -> Optional[str]: ...
    def column_gold_keywords(self, dataset: str, column: str) -> tuple[str, ...]: ...
    def owner(self, dataset: str) -> Optional[str]: ...
    def domain(self, dataset: str) -> Optional[str]: ...
    def known_terms(self) -> list[str]: ...


class WorldCatalogContext:
    """Catalog context backed by the demo world's ground truth."""

    def __init__(self, world: Any):
        self.world = world

    def dataset_name(self, urn: str) -> Optional[str]:
        try:
            return self.world.by_urn(urn).name
        except KeyError:
            return None

    def columns(self, dataset: str) -> list[str]:
        ds = self.world.datasets.get(dataset)
        return [c.name for c in ds.columns] if ds else []

    def column_pii(self, dataset: str, column: str) -> Optional[str]:
        return self.world.pii_type(dataset, column)

    def column_term(self, dataset: str, column: str) -> Optional[str]:
        return self.world.term_for(dataset, column)

    def column_gold_keywords(self, dataset: str, column: str) -> tuple[str, ...]:
        col = self.world.column(dataset, column)
        return col.gold_keywords if col else ()

    def owner(self, dataset: str) -> Optional[str]:
        ds = self.world.datasets.get(dataset)
        return ds.owner if ds else None

    def domain(self, dataset: str) -> Optional[str]:
        ds = self.world.datasets.get(dataset)
        return ds.domain if ds else None

    def known_terms(self) -> list[str]:
        return self.world.term_names()


# -- action parsing -----------------------------------------------------------


@dataclass
class Action:
    """The catalog intent of an observed tool call, parsed from its args."""

    agent_id: str
    tool: str
    op: str
    entity_urn: Optional[str] = None
    columns: list[str] = field(default_factory=list)
    description: Optional[str] = None
    operation: Optional[str] = None
    pii_types: list[str] = field(default_factory=list)
    term_names: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    event_id: Optional[str] = None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _pii_type_from_tag(urn: str) -> Optional[str]:
    """urn:li:tag:pii-email -> 'email'; non-pii tags -> None."""
    tail = urn.rsplit(":", 1)[-1].lower()
    if "pii" not in tail:
        return None
    body = tail.replace("pii-", "").replace("pii", "").strip("-_")
    return body.replace("-", "_") or "pii"


def _name_from_urn(urn: str) -> str:
    return urn.rsplit(":", 1)[-1]


def parse_action(event: ObservationEvent) -> Action:
    a = event.args or {}
    entity = a.get("entity_urn") or (_as_list(a.get("entity_urns")) or [None])[0]
    columns = _as_list(a.get("column_path")) + _as_list(a.get("column_paths"))
    tags = _as_list(a.get("tag_urns"))
    pii_types = [t for t in (_pii_type_from_tag(u) for u in tags) if t]
    terms = [_name_from_urn(u) for u in _as_list(a.get("term_urns"))]
    owners = [_name_from_urn(u) for u in _as_list(a.get("owner_urns"))]
    domains = [_name_from_urn(u) for u in _as_list(a.get("domain_urns")) + _as_list(a.get("domain_urn"))]
    return Action(
        agent_id=event.agent_id,
        tool=event.tool,
        op=event.op,
        entity_urn=entity,
        columns=[c for c in columns if c],
        description=a.get("description"),
        operation=a.get("operation"),
        pii_types=pii_types,
        term_names=terms,
        owners=owners,
        domains=domains,
        event_id=event.event_id,
    )


# -- findings -----------------------------------------------------------------


class Finding(BaseModel):
    """One catalog-grounded problem with one observed action."""

    finding_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    event_id: Optional[str] = None
    check_type: str
    severity: str = SEV_WARN
    verdict: str = FAIL
    entity_urn: Optional[str] = None
    column: Optional[str] = None
    reason: str = ""
    ts: float = Field(default_factory=time.time)


# -- evaluators ---------------------------------------------------------------


def _term_words(term: str) -> set[str]:
    return {w for w in term.lower().split() if len(w) > 2}


def _mentions_term(text: str, term: str) -> bool:
    """A description mentions a term if the whole phrase or all its salient words appear."""
    low = text.lower()
    if term.lower() in low:
        return True
    words = _term_words(term)
    return bool(words) and all(w in low for w in words)


def _dataset_or_none(action: Action, ctx: CatalogContext) -> Optional[str]:
    if not action.entity_urn:
        return None
    return ctx.dataset_name(action.entity_urn)


def check_undefined_column(action: Action, ctx: CatalogContext, dataset: str) -> list[Finding]:
    if action.operation == "remove":
        return []
    known = set(ctx.columns(dataset))
    out = []
    for col in action.columns:
        if known and col not in known:
            out.append(Finding(
                agent_id=action.agent_id, event_id=action.event_id,
                check_type=CHECK_UNDEFINED_COLUMN, severity=SEV_HARMFUL,
                entity_urn=action.entity_urn, column=col,
                reason=(f"wrote metadata onto column '{col}', which is not in the "
                        f"schema of {dataset} (columns: {', '.join(sorted(known))})"),
            ))
    return out


def check_description(action: Action, ctx: CatalogContext, dataset: str) -> list[Finding]:
    """Glossary conflict (harmful) or missing-concept quality (warn)."""
    desc = (action.description or "").strip()
    if not desc or action.operation == "remove":
        return []
    known = set(ctx.columns(dataset))
    out = []
    for col in action.columns:
        if known and col not in known:
            continue  # undefined-column check owns this case
        own_term = ctx.column_term(dataset, col)
        # glossary conflict: the description asserts a DIFFERENT catalog term
        conflict = None
        for other in ctx.known_terms():
            if own_term and other == own_term:
                continue
            if _mentions_term(desc, other) and not (own_term and _mentions_term(desc, own_term)):
                conflict = other
                break
        if conflict is not None:
            own = f"its glossary term '{own_term}'" if own_term else "no such glossary term"
            out.append(Finding(
                agent_id=action.agent_id, event_id=action.event_id,
                check_type=CHECK_GLOSSARY_CONFLICT, severity=SEV_HARMFUL,
                entity_urn=action.entity_urn, column=col,
                reason=(f"description of '{col}' asserts the glossary term "
                        f"'{conflict}', but the catalog defines this column as {own}"),
            ))
            continue
        # quality: description omits the column's expected concept
        gold = ctx.column_gold_keywords(dataset, col)
        if gold and not any(k in desc.lower() for k in gold):
            out.append(Finding(
                agent_id=action.agent_id, event_id=action.event_id,
                check_type=CHECK_LOW_QUALITY, severity=SEV_WARN,
                entity_urn=action.entity_urn, column=col,
                reason=(f"description of '{col}' does not mention its expected "
                        f"concept (one of: {', '.join(gold)})"),
            ))
    return out


def check_pii_scope(action: Action, ctx: CatalogContext, dataset: str) -> list[Finding]:
    if not action.pii_types:
        return []
    out = []
    targets = action.columns or [None]
    for col in targets:
        if col is None:
            continue
        truth = ctx.column_pii(dataset, col)
        for claimed in action.pii_types:
            if truth is None:
                out.append(Finding(
                    agent_id=action.agent_id, event_id=action.event_id,
                    check_type=CHECK_PII_SCOPE, severity=SEV_HARMFUL,
                    entity_urn=action.entity_urn, column=col,
                    reason=(f"flagged column '{col}' as PII ({claimed}), but the "
                            f"catalog marks it non-sensitive"),
                ))
            elif claimed != truth:
                out.append(Finding(
                    agent_id=action.agent_id, event_id=action.event_id,
                    check_type=CHECK_PII_SCOPE, severity=SEV_HARMFUL,
                    entity_urn=action.entity_urn, column=col,
                    reason=(f"tagged column '{col}' as PII type '{claimed}', but the "
                            f"catalog PII type is '{truth}'"),
                ))
    return out


def _norm(s: str) -> str:
    return s.strip().lower().replace("_", "-")


def check_governance(action: Action, ctx: CatalogContext, dataset: str) -> list[Finding]:
    out = []
    truth_owner = ctx.owner(dataset)
    for owner in action.owners:
        if truth_owner and _norm(owner) != _norm(truth_owner):
            out.append(Finding(
                agent_id=action.agent_id, event_id=action.event_id,
                check_type=CHECK_WRONG_OWNER, severity=SEV_HARMFUL,
                entity_urn=action.entity_urn,
                reason=(f"assigned owner '{owner}' to {dataset}, but the catalog "
                        f"owner is '{truth_owner}'"),
            ))
    truth_domain = ctx.domain(dataset)
    for domain in action.domains:
        if truth_domain and _norm(domain) != _norm(truth_domain):
            out.append(Finding(
                agent_id=action.agent_id, event_id=action.event_id,
                check_type=CHECK_WRONG_DOMAIN, severity=SEV_HARMFUL,
                entity_urn=action.entity_urn,
                reason=(f"assigned domain '{domain}' to {dataset}, but the catalog "
                        f"domain is '{truth_domain}'"),
            ))
    return out


def _is_removal(action: Action) -> bool:
    """Removing metadata is not asserting a catalog fact, so it is not graded."""
    return action.tool.startswith("remove_") or action.operation == "remove"


def ground_event(event: ObservationEvent, ctx: CatalogContext) -> list[Finding]:
    """All catalog-grounded findings for one observed action. Empty = clean."""
    action = parse_action(event)
    if _is_removal(action):
        return []
    dataset = _dataset_or_none(action, ctx)
    if dataset is None:
        return []  # not an entity we can ground against
    findings: list[Finding] = []
    findings += check_undefined_column(action, ctx, dataset)
    findings += check_description(action, ctx, dataset)
    findings += check_pii_scope(action, ctx, dataset)
    findings += check_governance(action, ctx, dataset)
    return findings


# -- finding store ------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    ts         REAL NOT NULL,
    agent_id   TEXT NOT NULL,
    event_id   TEXT,
    check_type TEXT NOT NULL,
    severity   TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    entity_urn TEXT,
    column     TEXT,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_find_agent ON findings (agent_id, ts);
CREATE INDEX IF NOT EXISTS idx_find_check ON findings (check_type);
"""


class FindingStore:
    """SQLite store of grounded findings. One writer, many readers (WAL)."""

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

    def __enter__(self) -> "FindingStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def record(self, finding: Finding) -> Finding:
        self._conn.execute(
            "INSERT INTO findings (finding_id, ts, agent_id, event_id, check_type,"
            " severity, verdict, entity_urn, column, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                finding.finding_id, finding.ts, finding.agent_id, finding.event_id,
                finding.check_type, finding.severity, finding.verdict,
                finding.entity_urn, finding.column, finding.reason,
            ),
        )
        self._conn.commit()
        return finding

    def findings(
        self,
        agent_id: Optional[str] = None,
        check_type: Optional[str] = None,
        severity: Optional[str] = None,
    ) -> list[Finding]:
        sql = "SELECT * FROM findings WHERE 1=1"
        params: list[Any] = []
        if agent_id is not None:
            sql += " AND agent_id=?"
            params.append(agent_id)
        if check_type is not None:
            sql += " AND check_type=?"
            params.append(check_type)
        if severity is not None:
            sql += " AND severity=?"
            params.append(severity)
        sql += " ORDER BY ts"
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_finding(r) for r in rows]

    def summary(self) -> dict[str, dict[str, int]]:
        """Per-agent finding counts: total, harmful, warn."""
        rows = self._conn.execute(
            "SELECT agent_id,"
            " COUNT(*) AS total,"
            " SUM(CASE WHEN severity='harmful' THEN 1 ELSE 0 END) AS harmful,"
            " SUM(CASE WHEN severity='warn' THEN 1 ELSE 0 END) AS warn"
            " FROM findings GROUP BY agent_id"
        ).fetchall()
        return {
            r["agent_id"]: {
                "total": r["total"], "harmful": r["harmful"] or 0, "warn": r["warn"] or 0,
            }
            for r in rows
        }

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.findings())


def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        finding_id=row["finding_id"], ts=row["ts"], agent_id=row["agent_id"],
        event_id=row["event_id"], check_type=row["check_type"],
        severity=row["severity"], verdict=row["verdict"],
        entity_urn=row["entity_urn"], column=row["column"], reason=row["reason"],
    )


def ground_events(
    events: list[ObservationEvent], ctx: CatalogContext, store: Optional[FindingStore] = None
) -> list[Finding]:
    """Ground a batch of observed actions; record findings if a store is given."""
    out: list[Finding] = []
    for event in events:
        for finding in ground_event(event, ctx):
            if store is not None:
                store.record(finding)
            out.append(finding)
    return out
