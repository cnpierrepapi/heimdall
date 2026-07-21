"""Settlement engine: match ground-truth events to open claims and score them.

Convention that keeps scoring uniform across claim types: a claim's
`confidence` is always P(the claim's stated prediction is true). Agents state
the direction they believe (e.g. will_break: true), so confidence >= 0.5 by
construction. Settlement then only has to decide whether the statement turned
out true; the Brier contribution is (confidence - truth)^2 for every claim
type, and `correct` simply records whether the statement was true.

Ground truth arrives as events (from the simulator here; from real assertion
runs, incident workflows, and steward reviews in production). Events are
persisted next to the claims for auditability.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field

from .claims import (
    BLAST_RADIUS,
    ENRICHMENT,
    FRESHNESS_SLA,
    ROOT_CAUSE,
    Claim,
    ClaimStore,
)

# Event types emitted by the simulator (and, in production, by observers of
# real DataHub assertion runs / incident workflows / steward actions).
ASSERTION_RESULT = "assertion_result"  # payload: {"passed": bool}
SLA_OUTCOME = "sla_outcome"  # payload: {"missed": bool}
INCIDENT_RESOLVED = "incident_resolved"  # payload: {"root_cause_urn": str}
STEWARD_REVIEW = "steward_review"  # payload: {"column": str, "verdict": str}

VERDICT_ACCEPTED = "accepted"
VERDICT_EDITED = "edited"
VERDICT_REVERTED = "reverted"


class GroundTruthEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    entity_urn: str
    payload: dict[str, Any]
    ts: float = Field(default_factory=time.time)


_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_urn TEXT NOT NULL,
    payload    TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events (entity_urn);
"""


class SettlementEngine:
    """Consumes ground-truth events, settles matching open claims."""

    def __init__(self, store: ClaimStore):
        self.store = store
        self._conn = sqlite3.connect(store.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_EVENTS_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def process_event(self, event: GroundTruthEvent) -> list[Claim]:
        """Persist the event and settle every open claim it resolves."""
        self._conn.execute(
            "INSERT INTO events (event_id, event_type, entity_urn, payload, ts)"
            " VALUES (?,?,?,?,?)",
            (
                event.event_id,
                event.event_type,
                event.entity_urn,
                json.dumps(event.payload),
                event.ts,
            ),
        )
        self._conn.commit()

        settled: list[Claim] = []
        for claim in self.store.unsettled():
            if claim.created_ts > event.ts:
                continue  # claims cannot be settled by events that predate them
            truth = _statement_truth(claim, event)
            if truth is None:
                continue
            settled.append(
                self.store.settle(
                    claim.claim_id,
                    outcome={"event_id": event.event_id, **event.payload},
                    correct=truth,
                    settled_ts=event.ts,
                )
            )
        return settled

    def events(self) -> list[GroundTruthEvent]:
        rows = self._conn.execute("SELECT * FROM events ORDER BY ts").fetchall()
        return [
            GroundTruthEvent(
                event_id=r[0],
                event_type=r[1],
                entity_urn=r[2],
                payload=json.loads(r[3]),
                ts=r[4],
            )
            for r in rows
        ]


def _statement_truth(claim: Claim, event: GroundTruthEvent) -> Optional[bool]:
    """Does this event settle this claim, and was the claim's statement true?

    Returns None when the event does not resolve the claim.
    """
    if claim.claim_type == BLAST_RADIUS:
        if event.entity_urn != claim.entity_urn:
            return None
        if event.event_type == ASSERTION_RESULT:
            broke = not event.payload["passed"]
        elif event.event_type == SLA_OUTCOME:
            broke = bool(event.payload["missed"])
        else:
            return None
        return bool(claim.prediction["will_break"]) == broke

    if claim.claim_type == FRESHNESS_SLA:
        if (
            event.event_type != SLA_OUTCOME
            or event.entity_urn != claim.entity_urn
        ):
            return None
        missed = bool(event.payload["missed"])
        return bool(claim.prediction["will_miss_sla"]) == missed

    if claim.claim_type == ROOT_CAUSE:
        if (
            event.event_type != INCIDENT_RESOLVED
            or event.entity_urn != claim.entity_urn
        ):
            return None
        return claim.prediction["root_cause_urn"] == event.payload["root_cause_urn"]

    if claim.claim_type == ENRICHMENT:
        if (
            event.event_type != STEWARD_REVIEW
            or event.entity_urn != claim.entity_urn
        ):
            return None
        if event.payload.get("column") != claim.prediction.get("column"):
            return None
        # Reviews are of a specific proposed description. When the event
        # names a claim, it settles only that claim; otherwise two agents
        # proposing different text for the same column would share one
        # verdict.
        target = event.payload.get("claim_id")
        if target is not None and target != claim.claim_id:
            return None
        return event.payload["verdict"] == VERDICT_ACCEPTED

    return None


# -- per-agent aggregates ----------------------------------------------------


def brier(claim: Claim) -> float:
    assert claim.correct is not None
    return (claim.confidence - (1.0 if claim.correct else 0.0)) ** 2


def agent_stats(store: ClaimStore, bins: int = 10) -> dict[str, dict[str, Any]]:
    """Settled-claim statistics per agent: win rate, Brier, calibration."""
    stats: dict[str, dict[str, Any]] = {}
    for agent_id in store.agent_ids():
        settled = store.claims(agent_id=agent_id, settled=True)
        entry: dict[str, Any] = {
            "n_total": len(store.claims(agent_id=agent_id)),
            "n_settled": len(settled),
            "wins": sum(1 for c in settled if c.correct),
        }
        if settled:
            entry["win_rate"] = entry["wins"] / len(settled)
            entry["brier_mean"] = sum(brier(c) for c in settled) / len(settled)
            entry["calibration"] = _calibration_bins(settled, bins)
            entry["ece"] = sum(
                b["n"] * abs(b["mean_confidence"] - b["frac_true"])
                for b in entry["calibration"]
            ) / len(settled)
        else:
            entry["win_rate"] = None
            entry["brier_mean"] = None
            entry["calibration"] = []
            entry["ece"] = None
        stats[agent_id] = entry
    return stats


def _calibration_bins(settled: list[Claim], bins: int) -> list[dict[str, Any]]:
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        members = [
            c
            for c in settled
            if lo <= c.confidence < hi or (i == bins - 1 and c.confidence == 1.0)
        ]
        if not members:
            continue
        out.append(
            {
                "bin_low": lo,
                "bin_high": hi,
                "n": len(members),
                "mean_confidence": sum(c.confidence for c in members) / len(members),
                "frac_true": sum(1 for c in members if c.correct) / len(members),
            }
        )
    return out
