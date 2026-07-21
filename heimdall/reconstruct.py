"""Turn a reconstructed change stream into settled claims per writer.

P1 (`provenance.py`) reads a catalog's real change history. This layer decides,
for each metadata write in that history, whether it was an assertion that held
up or one that a steward undid, and records it as a claim in the same ledger the
simulated agents use, so the same skill-vs-luck test and trust score apply to
real writers pulled from a live catalog.

The settlement rule (agreed design):

  revert (heavy)   a LATER change by a DIFFERENT actor clears the value
                   (documentation blanked or removed) or overwrites it with
                   different text. This is a steward correcting the writer, so
                   the write settles wrong at the time of the correction.
  survived (light) the write is still the current value after a survival window
                   and nobody contradicted it. Weak positive: it settles right,
                   but see below for why survival is deliberately cheap.
  unsettled        the write is still current but younger than the window; its
                   outcome is not known yet, so it stays open.

Survival is a weak positive by construction, not by a hand-tuned weight:

  1. Reconstructed claims carry an implicit confidence below 1.0. Under the
     Brier score a wrong claim at confidence c costs c^2 while a right one gains
     only (1-c)^2, so at 0.75 a revert weighs about nine times a survival. That
     asymmetry is the "revert heavy, survival light" rule with no look-ahead.
  2. The skill verdict scores wins against the pooled acceptance rate across all
     reconstructed claims. When survival is the norm the pooled rate is high, so
     surviving does not look skilled; only being reverted less than the pool (or
     out-surviving a weak rival) earns a skilled verdict.
  3. Trust shrinks toward neutral by settled-claim count, so a handful of
     survivals cannot mint a high score.

A write superseded by the SAME actor (the writer revising its own text) is not a
steward signal; the earlier write is left unsettled rather than counted.
"""

from __future__ import annotations

import time
from typing import Iterable, NamedTuple, Optional

from .claims import ENRICHMENT, Claim, ClaimStore
from .provenance import ProvChange

SURVIVAL_DAYS = 7
DEFAULT_RECON_CONFIDENCE = 0.75  # lower makes reverts weigh more (see module doc)
MODEL_RECONSTRUCTED = "reconstructed"

# Writers whose edits are heimdall's own writeback, not authored claims to
# score. Tag targets matching these are skipped.
EXCLUDED_TARGET_SUBSTRINGS = ("heimdall",)

_CLEAR_OPS = {"REMOVE"}


def recon_agent_id(actor: str, work_kind: str) -> str:
    """Identity is the work a writer does: (actor, work kind) -> one profile.

    The actor urn is shortened to its last segment for readability; a shared
    service account doing several kinds still decomposes into one id per kind.
    """
    short = actor.split(":")[-1] if actor else "unknown"
    return f"{short}/{work_kind}"


class ReconOutcome(NamedTuple):
    """A reconstructed claim and the outcome to settle it with (if any)."""

    claim: Claim
    correct: Optional[bool]  # None = leave unsettled
    settled_ts: Optional[float]


def _is_write(change: ProvChange) -> bool:
    """An event that asserts a value (vs one that clears it)."""
    if change.operation in _CLEAR_OPS:
        return False
    if change.category == "DOCUMENTATION":
        return bool(change.value)  # non-empty text
    return change.operation in ("ADD", "MODIFY")


def _is_clear(change: ProvChange) -> bool:
    return change.is_clear


def _group_key(change: ProvChange) -> tuple[str, Optional[str], Optional[str]]:
    """The thing being asserted about: dataset, field, and target urn."""
    return (change.dataset_urn, change.field, change.target)


def _excluded(change: ProvChange) -> bool:
    if change.target:
        low = change.target.lower()
        return any(s in low for s in EXCLUDED_TARGET_SUBSTRINGS)
    return False


def reconstruct(
    changes: Iterable[ProvChange],
    now_ts: Optional[float] = None,
    survival_days: int = SURVIVAL_DAYS,
    confidence: float = DEFAULT_RECON_CONFIDENCE,
) -> list[ReconOutcome]:
    """Decide a claim and outcome for every write in the change stream.

    Pure: no store, no clock beyond ``now_ts`` (defaults to the latest change so
    historical reconstruction is deterministic).
    """
    changes = [c for c in changes if not _excluded(c)]
    if now_ts is None:
        now_ts = max((c.ts for c in changes), default=time.time())
    window = survival_days * 86400

    groups: dict[tuple[str, Optional[str], Optional[str]], list[ProvChange]] = {}
    for c in changes:
        groups.setdefault(_group_key(c), []).append(c)

    out: list[ReconOutcome] = []
    for _, events in groups.items():
        events.sort(key=lambda c: c.ts)
        for i, ev in enumerate(events):
            if not _is_write(ev):
                continue
            correct, settled_ts = _outcome(ev, events[i + 1 :], now_ts, window)
            out.append(ReconOutcome(_to_claim(ev, confidence), correct, settled_ts))
    return out


def _outcome(
    write: ProvChange,
    later: list[ProvChange],
    now_ts: float,
    window: float,
) -> tuple[Optional[bool], Optional[float]]:
    """Classify one write against the events that followed it in its group."""
    for nxt in later:
        if nxt.actor == write.actor:
            # the writer revised its own work: not a steward signal, stop here
            return (None, None)
        contradicts = _is_clear(nxt) or (
            nxt.category == "DOCUMENTATION"
            and _is_write(nxt)
            and (nxt.value or "") != (write.value or "")
        )
        if contradicts:
            return (False, nxt.ts)  # reverted by a different actor
        # a different actor reaffirming the same value: keep looking
    # nothing contradicted it; did it survive the window?
    if now_ts - write.ts >= window:
        return (True, write.ts + window)
    return (None, None)  # still fresh


def _to_claim(change: ProvChange, confidence: float) -> Claim:
    return Claim(
        agent_id=recon_agent_id(change.actor, change.work_kind),
        model_id=MODEL_RECONSTRUCTED,
        claim_type=ENRICHMENT,
        entity_urn=change.dataset_urn,
        prediction={
            "kind": change.work_kind,
            "column": change.field,
            "target": change.target,
            "value": change.value,
        },
        confidence=confidence,
        evidence=[f"reconstructed from timeline: {change.operation} by {change.actor}"],
        created_ts=change.ts,
    )


def load_into_store(
    store: ClaimStore,
    changes: Iterable[ProvChange],
    now_ts: Optional[float] = None,
    survival_days: int = SURVIVAL_DAYS,
    confidence: float = DEFAULT_RECON_CONFIDENCE,
) -> dict[str, int]:
    """Record reconstructed claims into the ledger and settle the resolved ones.

    Returns a small summary: writes recorded, settled, reverted, survived.
    """
    outcomes = reconstruct(changes, now_ts, survival_days, confidence)
    recorded = settled = reverted = survived = 0
    for oc in outcomes:
        store.record(oc.claim)
        recorded += 1
        if oc.correct is None:
            continue
        store.settle(
            oc.claim.claim_id,
            outcome={"source": "timeline_reconstruction", "reverted": not oc.correct},
            correct=oc.correct,
            settled_ts=oc.settled_ts,
        )
        settled += 1
        if oc.correct:
            survived += 1
        else:
            reverted += 1
    return {
        "recorded": recorded,
        "settled": settled,
        "reverted": reverted,
        "survived": survived,
    }
