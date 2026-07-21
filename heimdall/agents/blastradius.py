"""Blast-radius forecaster.

Given a schema change (a column dropped from a dataset), predict which
downstream assets will actually break. The agent is a scaffolded pipeline,
not a free-roaming loop: evidence gathering is fixed MCP calls (downstream
lineage, then each candidate's schema), and the model exercises judgment in
exactly one place, over exactly the evidence gathered. Every failure is
attributable to that judgment, which is what makes the settled record
meaningful.

The hard part of the task is real: sitting downstream of a change does not
imply breakage. A candidate breaks only if it (transitively) consumes the
dropped column, and the agent must infer that from schemas, column names,
and descriptions read through the MCP server.
"""

from __future__ import annotations

import time
from typing import Any

from ..claims import BLAST_RADIUS, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from ..simulator.runner import normalize_directional
from .common import as_text, clamp_confidence, extract_dataset_urns

_SYSTEM = """You are a senior data reliability engineer assessing the impact of a schema change in a data warehouse.

A column is being dropped from an upstream table. For EACH downstream candidate table you are given, decide whether its build will BREAK (fail or produce wrong results) because of this change.

Key principle: a table breaks only if it consumes the dropped column, directly or through intermediate tables. Being downstream is NOT enough. Use the schemas: if a candidate (or the path to it) has a column that plausibly derives from the dropped column (matching or related name/meaning), it likely breaks. If the candidate only uses OTHER columns from the changed table's lineage, it likely survives.

Reply with ONLY a JSON object, no prose:
{"assessments": [{"dataset_urn": "...", "will_break": true|false, "confidence": 0.5-0.95, "reason": "one short sentence"}]}

Confidence is your probability that your will_break verdict is right. Include every candidate exactly once."""


class BlastRadiusAgent:
    agent_id = "blast-radius"

    def __init__(self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "blast-radius"):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    # -- evidence gathering (fixed steps, no model involvement) -------------

    def downstream_candidates(self, changed_urn: str) -> list[str]:
        lineage = self.mcp.get_lineage(changed_urn, upstream=False, max_hops=3)
        return extract_dataset_urns(lineage, exclude=[changed_urn])

    def schema_evidence(self, urn: str) -> str:
        return as_text(self.mcp.list_schema_fields(urn), 2500)

    # -- the one judgment call ----------------------------------------------

    def forecast(
        self, changed_urn: str, dropped_column: str, model_id: str = ""
    ) -> list[Claim]:
        candidates = self.downstream_candidates(changed_urn)
        if not candidates:
            return []

        evidence_blocks = []
        for urn in candidates:
            evidence_blocks.append(
                f"CANDIDATE: {urn}\nSCHEMA: {self.schema_evidence(urn)}"
            )
        changed_schema = self.schema_evidence(changed_urn)

        user = (
            f"SCHEMA CHANGE: column `{dropped_column}` is being DROPPED from:\n"
            f"{changed_urn}\n"
            f"Changed table schema (before the drop): {changed_schema}\n\n"
            f"DOWNSTREAM CANDIDATES ({len(candidates)}):\n\n"
            + "\n\n".join(evidence_blocks)
        )

        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=2500)
        assessments = {
            a["dataset_urn"]: a for a in parsed.get("assessments", [])
        }

        claims: list[Claim] = []
        now = time.time()
        for urn in candidates:
            a: dict[str, Any] | None = assessments.get(urn)
            if a is None:
                # the model skipped a candidate: record an explicit abstention
                # at minimum confidence rather than silently claiming nothing
                will_break, conf, reason = False, 0.5, "model omitted candidate"
            else:
                will_break = bool(a.get("will_break", False))
                conf = clamp_confidence(a.get("confidence"), 0.0, 1.0)
                reason = str(a.get("reason", ""))[:300]
            will_break, conf = normalize_directional(will_break, conf)
            conf = min(max(conf, 0.5), 0.95)
            claims.append(
                Claim(
                    agent_id=self.agent_id,
                    model_id=model_id or self.llm.model,
                    claim_type=BLAST_RADIUS,
                    entity_urn=urn,
                    prediction={
                        "will_break": will_break,
                        "changed_dataset": changed_urn,
                        "dropped_column": dropped_column,
                        "reason": reason,
                    },
                    confidence=conf,
                    evidence=[changed_urn],
                    created_ts=now,
                )
            )
        return claims
