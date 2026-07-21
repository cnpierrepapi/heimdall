"""Incident triage: name the root cause of a data incident.

Given a broken or stale downstream asset, walk its upstream lineage through
MCP to get the candidate set, batch-read the candidates' catalog context,
and combine that with operational telemetry (which feeds landed late) in one
judgment call that names a single root-cause dataset.

Root-cause claims are where the skill test bites hardest: the null win
probability is 1/n_candidates, not one half, so a triage agent that guesses
among five upstreams wins 20% of the time by luck and the Monte Carlo null
knows it. The prediction records n_candidates for exactly that reason.
"""

from __future__ import annotations

import time

from ..claims import ROOT_CAUSE, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from .common import as_text, clamp_confidence, extract_dataset_urns

_SYSTEM = """You are the on-call data engineer triaging a data incident in a warehouse.

A downstream asset is broken or stale. You get its upstream lineage candidates with catalog context, plus operational telemetry about recent load arrivals. Pick the SINGLE most likely root-cause dataset. Prefer the deepest upstream cause consistent with the telemetry: a raw feed that landed late explains staleness in everything built on it, while an intermediate table is only the cause if its own inputs were healthy.

Reply with ONLY a JSON object, no prose:
{"root_cause_urn": "...", "confidence": 0.05-0.95, "reason": "one short sentence"}

root_cause_urn MUST be exactly one of the candidate urns. Confidence is your probability that this pick is the true root cause."""


class TriageAgent:
    agent_id = "incident-triage"

    def __init__(
        self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "incident-triage"
    ):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    # -- evidence gathering (fixed steps, no model involvement) -------------

    def upstream_candidates(self, affected_urn: str) -> list[str]:
        lineage = self.mcp.get_lineage(affected_urn, upstream=True, max_hops=3)
        return extract_dataset_urns(lineage, exclude=[affected_urn])

    def candidate_evidence(self, urns: list[str]) -> str:
        return as_text(self.mcp.get_entities(urns), limit=4000)

    # -- the one judgment call ----------------------------------------------

    def diagnose(
        self,
        incident_urn: str,
        affected_urn: str,
        symptom: str,
        telemetry: list[str],
        model_id: str = "",
    ) -> Claim | None:
        candidates = self.upstream_candidates(affected_urn)
        if not candidates:
            return None

        user = (
            f"INCIDENT: {incident_urn}\n"
            f"AFFECTED ASSET: {affected_urn}\n"
            f"SYMPTOM: {symptom}\n\n"
            f"UPSTREAM CANDIDATES ({len(candidates)}):\n"
            + "\n".join(f"  {u}" for u in candidates)
            + f"\n\nCANDIDATE CONTEXT:\n{self.candidate_evidence(candidates)}\n\n"
            f"OPERATIONAL TELEMETRY:\n" + "\n".join(f"  {t}" for t in telemetry)
        )

        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=500)
        pick = parsed.get("root_cause_urn")
        conf = clamp_confidence(parsed.get("confidence"), 0.05, 0.95)
        reason = str(parsed.get("reason", ""))[:300]
        if pick not in candidates:
            # an invalid pick earns no benefit of the doubt: fall back to the
            # nearest candidate at the uniform prior, flagged as such
            pick = candidates[0]
            conf = 1.0 / len(candidates)
            reason = "model picked a non-candidate; recorded at uniform prior"

        return Claim(
            agent_id=self.agent_id,
            model_id=model_id or self.llm.model,
            claim_type=ROOT_CAUSE,
            entity_urn=incident_urn,
            prediction={
                "root_cause_urn": pick,
                "n_candidates": len(candidates),
                "affected_urn": affected_urn,
                "reason": reason,
            },
            confidence=conf,
            evidence=[affected_urn],
            created_ts=time.time(),
        )
