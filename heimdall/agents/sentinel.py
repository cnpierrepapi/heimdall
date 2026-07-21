"""Freshness sentinel: forecast whether a feed will miss its next SLA.

Unlike the blast-radius agent, whose claims settle within minutes, the
sentinel makes genuine forward-looking forecasts: the claim is recorded
before the outcome exists and settles only when the SLA window closes.
Evidence is the dataset's catalog context read through MCP plus the arrival
history any observer of the pipeline could legitimately have seen. The
judgment is one LLM pass over that history: base rate, trend, and streaks
against a hard deadline.
"""

from __future__ import annotations

import time

from ..claims import FRESHNESS_SLA, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from ..simulator.runner import normalize_directional
from .common import as_text, clamp_confidence

_SYSTEM = """You are a data pipeline SRE forecasting whether a feed's next load will MISS its SLA deadline.

You get the dataset's catalog context and its recent arrival history, oldest day first (each day: ON_TIME or LATE). Weigh the base rate and any trend or streak; recent days matter more than old ones. A feed that has started failing repeatedly tends to keep failing until someone fixes it; a single old miss in an otherwise clean history usually does not repeat.

Reply with ONLY a JSON object, no prose:
{"will_miss_sla": true|false, "confidence": 0.5-0.95, "reason": "one short sentence"}

Confidence is your probability that your will_miss_sla verdict is right. With little history, stay near 0.5-0.65."""


class FreshnessSentinelAgent:
    agent_id = "freshness-sentinel"

    def __init__(
        self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "freshness-sentinel"
    ):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    # -- evidence gathering (fixed steps, no model involvement) -------------

    def dataset_evidence(self, urn: str) -> str:
        return as_text(self.mcp.get_entities([urn]), limit=1200)

    # -- the one judgment call ----------------------------------------------

    def forecast(
        self,
        dataset_urn: str,
        history: list[bool],
        day: int,
        model_id: str = "",
    ) -> Claim:
        """history: prior days' outcomes, oldest first (True = the load was late)."""
        history_lines = [
            f"  day {i}: {'LATE' if late else 'ON_TIME'}"
            for i, late in enumerate(history)
        ]
        user = (
            f"DATASET:\n{self.dataset_evidence(dataset_urn)}\n\n"
            f"ARRIVAL HISTORY ({len(history)} prior days, oldest first):\n"
            + ("\n".join(history_lines) if history_lines else "  (no history yet)")
            + f"\n\nForecast: will day {day}'s load MISS its SLA?"
        )

        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=400)
        will_miss = bool(parsed.get("will_miss_sla", False))
        conf = clamp_confidence(parsed.get("confidence"), 0.0, 1.0)
        reason = str(parsed.get("reason", ""))[:300]
        will_miss, conf = normalize_directional(will_miss, conf)
        conf = min(max(conf, 0.5), 0.95)

        return Claim(
            agent_id=self.agent_id,
            model_id=model_id or self.llm.model,
            claim_type=FRESHNESS_SLA,
            entity_urn=dataset_urn,
            prediction={"will_miss_sla": will_miss, "day": day, "reason": reason},
            confidence=conf,
            evidence=[f"history_days={len(history)}"],
            created_ts=time.time(),
        )
