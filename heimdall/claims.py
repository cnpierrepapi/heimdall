"""Claim schema and SQLite-backed ledger store.

A claim is the atomic unit of heimdall: one falsifiable statement an agent
made about a catalog entity, with a confidence attached. Claims are recorded
at action time and settled later, when ground truth arrives. Every other
component (settlement, gateway, scoreboard) reads and writes this store.

SQLite keeps the whole system runnable from a fresh clone with zero services.
WAL mode lets the gateway and scoreboard read while agents write.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import BaseModel, Field, field_validator

# Claim types shipped with the reference agents. The store itself accepts any
# string, so new agent types do not require a schema change.
BLAST_RADIUS = "blast_radius"
FRESHNESS_SLA = "freshness_sla"
ENRICHMENT = "enrichment"
ROOT_CAUSE = "root_cause"

# Meta claim types: claims about other agents' claims and about the catalog's
# provenance. Directional, so the default 0.5 luck baseline applies.
REVERT_FORECAST = "revert_forecast"
ARBITRATION = "arbitration"
TAMPER_AUDIT = "tamper_audit"


class Claim(BaseModel):
    """One falsifiable statement by one agent about one entity."""

    claim_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    model_id: str = ""
    claim_type: str
    entity_urn: str
    prediction: dict[str, Any]
    confidence: float
    evidence: list[str] = Field(default_factory=list)
    created_ts: float = Field(default_factory=time.time)
    settled_ts: Optional[float] = None
    outcome: Optional[dict[str, Any]] = None
    correct: Optional[bool] = None

    @field_validator("confidence")
    @classmethod
    def _confidence_in_unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {v}")
        return v

    @property
    def settled(self) -> bool:
        return self.settled_ts is not None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    claim_id   TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    model_id   TEXT NOT NULL DEFAULT '',
    claim_type TEXT NOT NULL,
    entity_urn TEXT NOT NULL,
    prediction TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '[]',
    created_ts REAL NOT NULL,
    settled_ts REAL,
    outcome    TEXT,
    correct    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_claims_agent ON claims (agent_id);
CREATE INDEX IF NOT EXISTS idx_claims_open ON claims (settled_ts) WHERE settled_ts IS NULL;
CREATE INDEX IF NOT EXISTS idx_claims_entity ON claims (entity_urn);
"""


class AlreadySettledError(RuntimeError):
    pass


class ClaimStore:
    """SQLite ledger of claims. Safe for one writer, many readers (WAL)."""

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

    def __enter__(self) -> "ClaimStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def record(self, claim: Claim) -> Claim:
        self._conn.execute(
            "INSERT INTO claims (claim_id, agent_id, model_id, claim_type,"
            " entity_urn, prediction, confidence, evidence, created_ts,"
            " settled_ts, outcome, correct)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                claim.claim_id,
                claim.agent_id,
                claim.model_id,
                claim.claim_type,
                claim.entity_urn,
                json.dumps(claim.prediction),
                claim.confidence,
                json.dumps(claim.evidence),
                claim.created_ts,
                claim.settled_ts,
                json.dumps(claim.outcome) if claim.outcome is not None else None,
                None if claim.correct is None else int(claim.correct),
            ),
        )
        self._conn.commit()
        return claim

    def settle(
        self,
        claim_id: str,
        outcome: dict[str, Any],
        correct: bool,
        settled_ts: Optional[float] = None,
    ) -> Claim:
        existing = self.get(claim_id)
        if existing is None:
            raise KeyError(f"no such claim: {claim_id}")
        if existing.settled:
            raise AlreadySettledError(f"claim already settled: {claim_id}")
        ts = settled_ts if settled_ts is not None else time.time()
        self._conn.execute(
            "UPDATE claims SET settled_ts=?, outcome=?, correct=? WHERE claim_id=?",
            (ts, json.dumps(outcome), int(correct), claim_id),
        )
        self._conn.commit()
        settled = self.get(claim_id)
        assert settled is not None
        return settled

    # -- reads -------------------------------------------------------------

    def get(self, claim_id: str) -> Optional[Claim]:
        row = self._conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return _row_to_claim(row) if row is not None else None

    def claims(
        self,
        agent_id: Optional[str] = None,
        claim_type: Optional[str] = None,
        entity_urn: Optional[str] = None,
        settled: Optional[bool] = None,
    ) -> list[Claim]:
        sql = "SELECT * FROM claims WHERE 1=1"
        args: list[Any] = []
        if agent_id is not None:
            sql += " AND agent_id=?"
            args.append(agent_id)
        if claim_type is not None:
            sql += " AND claim_type=?"
            args.append(claim_type)
        if entity_urn is not None:
            sql += " AND entity_urn=?"
            args.append(entity_urn)
        if settled is True:
            sql += " AND settled_ts IS NOT NULL"
        elif settled is False:
            sql += " AND settled_ts IS NULL"
        sql += " ORDER BY created_ts"
        rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_claim(r) for r in rows]

    def unsettled(self, claim_type: Optional[str] = None) -> list[Claim]:
        return self.claims(claim_type=claim_type, settled=False)

    def agent_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT agent_id FROM claims ORDER BY agent_id"
        ).fetchall()
        return [r["agent_id"] for r in rows]

    def summary(self) -> dict[str, dict[str, int]]:
        """Per-agent counts: total, settled, correct."""
        rows = self._conn.execute(
            "SELECT agent_id,"
            " COUNT(*) AS total,"
            " SUM(CASE WHEN settled_ts IS NOT NULL THEN 1 ELSE 0 END) AS settled,"
            " SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS correct"
            " FROM claims GROUP BY agent_id"
        ).fetchall()
        return {
            r["agent_id"]: {
                "total": r["total"],
                "settled": r["settled"] or 0,
                "correct": r["correct"] or 0,
            }
            for r in rows
        }

    def __iter__(self) -> Iterator[Claim]:
        return iter(self.claims())


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        claim_id=row["claim_id"],
        agent_id=row["agent_id"],
        model_id=row["model_id"],
        claim_type=row["claim_type"],
        entity_urn=row["entity_urn"],
        prediction=json.loads(row["prediction"]),
        confidence=row["confidence"],
        evidence=json.loads(row["evidence"]),
        created_ts=row["created_ts"],
        settled_ts=row["settled_ts"],
        outcome=json.loads(row["outcome"]) if row["outcome"] is not None else None,
        correct=None if row["correct"] is None else bool(row["correct"]),
    )
