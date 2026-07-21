"""Disagreement arbiter: when two agents conflict, pick the one that holds.

Conflicts are found mechanically in the ledger: two open claims proposing
different values for the same target (same entity, same proposal kind, same
column where applicable). The arbiter reads both proposals and both authors'
settled records and commits to the one the steward will accept.

The claim settles off the chosen target: correct when the picked proposal is
accepted. n_candidates is recorded so the pick is honest about its odds.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..claims import ARBITRATION, Claim, ClaimStore
from ..llm import LLMClient
from ..settle import agent_stats
from .common import clamp_confidence

_SYSTEM = """You are arbitrating between conflicting metadata proposals for the same catalog target.

You get two proposals (A and B) for the same target from different agents, each with its author's settled track record. Exactly one can stand. Pick the proposal a careful data steward would ACCEPT, judging the content on its merits (specific and accurate beats generic filler; correct semantics beat name similarity) with the records as a prior.

Reply with ONLY a JSON object, no prose:
{"winner": "A" or "B", "confidence": 0.05-0.95}

Confidence is your probability that your pick is the accepted one."""


def find_conflicts(open_claims: list[Claim]) -> list[tuple[Claim, Claim]]:
    """Pairs of open claims disputing the same target with different values."""
    by_target: dict[tuple, list[Claim]] = {}
    for c in open_claims:
        kind = c.prediction.get("kind", "column_doc")
        key = (c.entity_urn, kind, c.prediction.get("column"))
        by_target.setdefault(key, []).append(c)

    pairs: list[tuple[Claim, Claim]] = []
    for group in by_target.values():
        if len(group) < 2:
            continue
        # first two distinct proposals from different agents form the dispute
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.agent_id != b.agent_id and _value(a) != _value(b):
                    pairs.append((a, b))
                    break
            else:
                continue
            break
    return pairs


def _value(c: Claim) -> Any:
    p = c.prediction
    return (
        p.get("description")
        or p.get("pii_type")
        or p.get("owner")
        or p.get("domain")
        or p.get("term")
    )


class DisagreementArbiterAgent:
    agent_id = "disagreement-arbiter"

    def __init__(self, llm: LLMClient, agent_id: str = "disagreement-arbiter"):
        self.llm = llm
        self.agent_id = agent_id

    def arbitrate(
        self, store: ClaimStore, a: Claim, b: Claim, model_id: str = ""
    ) -> Optional[Claim]:
        stats = agent_stats(store)

        def side(label: str, c: Claim) -> str:
            rec = stats.get(c.agent_id, {})
            record = (
                f"settled={rec.get('n_settled', 0)} "
                f"win_rate={rec.get('win_rate', 0) or 0:.2f}"
                if rec.get("n_settled")
                else "no settled record"
            )
            pred = {k: v for k, v in c.prediction.items() if k != "kind"}
            return (
                f"PROPOSAL {label} by {c.agent_id} ({record}), "
                f"stated confidence {c.confidence:.2f}: {pred}"
            )

        user = (
            f"TARGET: {a.entity_urn} "
            f"kind={a.prediction.get('kind', 'column_doc')} "
            f"column={a.prediction.get('column')}\n"
            f"{side('A', a)}\n{side('B', b)}"
        )
        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=300)
        pick = str(parsed.get("winner", "")).strip().upper()
        if pick not in ("A", "B"):
            return None  # refusing beats guessing
        winner, loser = (a, b) if pick == "A" else (b, a)
        return Claim(
            agent_id=self.agent_id,
            model_id=model_id or self.llm.model,
            claim_type=ARBITRATION,
            entity_urn=winner.entity_urn,
            prediction={
                "kind": "arbitration",
                "winner_claim_id": winner.claim_id,
                "loser_claim_id": loser.claim_id,
                "winner_agent": winner.agent_id,
                "n_candidates": 2,
            },
            confidence=clamp_confidence(parsed.get("confidence"), 0.5, 0.95),
            evidence=[winner.claim_id, loser.claim_id],
            created_ts=time.time(),
        )
