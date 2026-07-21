"""Revert predictor: forecast which incoming writes stewards will revert.

A meta agent whose subject matter is the agent economy itself. Given open
(unsettled) proposals from other agents, plus each author's settled track
record from the ledger, it predicts per proposal whether the steward will
revert it. The claim settles mechanically when the target claim settles:
reverted means the target settled wrong.

A calibrated revert predictor is what upgrades the gateway from a blunt
trust floor into triage: hold the writes it flags, wave the rest through.
Ground truth is free, the ledger already produces it.
"""

from __future__ import annotations

import time
from typing import Any

from ..claims import REVERT_FORECAST, Claim, ClaimStore
from ..llm import LLMClient
from ..settle import agent_stats
from .common import clamp_confidence
from ..simulator.runner import normalize_directional

_SYSTEM = """You are auditing incoming metadata proposals from AI agents before a human steward reviews them.

For EACH proposal you get: the proposal itself (kind, target, proposed value, the author's stated confidence) and the author agent's settled track record (settled count, win rate, mean Brier). Predict whether the steward will REVERT the proposal (reject it) or accept it.

Reason from the record and the content: authors with weak settled records revert often; filler or generic text gets reverted; flags on pseudonymous keys or coarse demographics get reverted; specific, evidence-grounded proposals from proven authors get accepted.

Reply with ONLY a JSON object, no prose:
{"forecasts": [{"claim_id": "...", "will_be_reverted": true/false, "confidence": 0.05-0.95}]}

Include every proposal exactly once. Confidence is your probability for YOUR stated direction."""


def _record_line(stats: dict[str, Any]) -> str:
    if not stats or not stats.get("n_settled"):
        return "no settled record"
    return (
        f"settled={stats['n_settled']} win_rate={stats.get('win_rate', 0):.2f} "
        f"brier={stats.get('brier_mean', 0):.3f}"
    )


class RevertPredictorAgent:
    agent_id = "revert-predictor"

    def __init__(self, llm: LLMClient, agent_id: str = "revert-predictor"):
        self.llm = llm
        self.agent_id = agent_id

    def forecast(
        self, store: ClaimStore, open_claims: list[Claim], model_id: str = ""
    ) -> list[Claim]:
        """One judgment call over a batch of open proposals by OTHER agents."""
        targets = [c for c in open_claims if c.agent_id != self.agent_id]
        if not targets:
            return []
        stats = agent_stats(store)

        lines = []
        for c in targets:
            pred = {k: v for k, v in c.prediction.items() if k != "kind"}
            lines.append(
                f"- claim_id={c.claim_id} author={c.agent_id} "
                f"({_record_line(stats.get(c.agent_id, {}))}) "
                f"kind={c.prediction.get('kind', 'column_doc')} "
                f"author_confidence={c.confidence:.2f} proposal={pred}"
            )
        user = f"OPEN PROPOSALS ({len(targets)}):\n" + "\n".join(lines)

        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=1500)
        by_id = {
            f.get("claim_id"): f
            for f in parsed.get("forecasts", [])
            if isinstance(f, dict)
        }

        claims: list[Claim] = []
        now = time.time()
        for target in targets:
            f = by_id.get(target.claim_id)
            if f is None:
                continue  # omitted answers are abstentions
            will_revert = bool(f.get("will_be_reverted", False))
            conf = clamp_confidence(f.get("confidence"), 0.0, 1.0)
            will_revert, conf = normalize_directional(will_revert, conf)
            conf = min(max(conf, 0.5), 0.95)
            claims.append(
                Claim(
                    agent_id=self.agent_id,
                    model_id=model_id or self.llm.model,
                    claim_type=REVERT_FORECAST,
                    entity_urn=target.entity_urn,
                    prediction={
                        "kind": "revert_forecast",
                        "target_claim_id": target.claim_id,
                        "author_agent": target.agent_id,
                        "will_be_reverted": will_revert,
                    },
                    confidence=conf,
                    evidence=[target.claim_id],
                    created_ts=now,
                )
            )
        return claims
