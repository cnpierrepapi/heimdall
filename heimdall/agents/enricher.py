"""Enricher: propose documentation for undocumented columns.

The agent discovers its own work through MCP (schema fields that carry no
description), gathers the evidence that gives a column meaning (sibling
columns, upstream schemas, the column-level derivations in lineage), and
drafts one description per undocumented column in a single judgment call.

Each proposal is a claim whose confidence is P(a steward accepts the text).
This is the claim type where "luck" has a real market rate: the null is the
pooled acceptance rate across all agents, so an agent only earns a skilled
verdict by beating the going standard, not a coin flip. Accepted proposals
can be written back to DataHub via update_description (apply), which is the
"real work" half of the loop.
"""

from __future__ import annotations

import time
from typing import Any

from ..claims import ENRICHMENT, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from .common import as_text, clamp_confidence, extract_dataset_urns

_SYSTEM = """You are a data steward writing column documentation for a warehouse catalog.

You get a table's schema (some columns documented, some not), the schemas of its direct upstream tables, and the lineage between them. For EACH undocumented column listed, write a one-sentence description of its real-world meaning: what quantity or identifier it holds, units if monetary or temporal, and where it comes from when the upstream columns make that clear. Be specific and factual; no filler like "this column stores data". Plain prose, no em dashes.

Reply with ONLY a JSON object, no prose:
{"proposals": [{"column": "...", "description": "...", "confidence": 0.05-0.95}]}

Confidence is your probability that a human steward would ACCEPT your description as accurate. Include every undocumented column exactly once."""


def undocumented_columns(schema: Any) -> list[str]:
    """Field paths with no usable description in a list_schema_fields result."""
    if not isinstance(schema, dict):
        return []
    return [
        f["fieldPath"]
        for f in schema.get("fields", [])
        if f.get("fieldPath") and not str(f.get("description") or "").strip()
    ]


class EnricherAgent:
    agent_id = "enricher"

    def __init__(self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "enricher"):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    # -- evidence gathering (fixed steps, no model involvement) -------------

    def upstream_evidence(self, urn: str) -> tuple[str, str]:
        """(lineage text, upstream schemas text) for the table's direct parents."""
        lineage = self.mcp.get_lineage(urn, upstream=True, max_hops=1)
        upstream_urns = extract_dataset_urns(lineage, exclude=[urn])
        blocks = []
        for up in upstream_urns:
            blocks.append(
                f"UPSTREAM: {up}\nSCHEMA: {as_text(self.mcp.list_schema_fields(up), 1500)}"
            )
        return as_text(lineage, 2000), "\n\n".join(blocks)

    # -- the one judgment call ----------------------------------------------

    def propose(self, dataset_urn: str, model_id: str = "") -> list[Claim]:
        schema = self.mcp.list_schema_fields(dataset_urn)
        targets = undocumented_columns(schema)
        if not targets:
            return []

        lineage_text, upstream_text = self.upstream_evidence(dataset_urn)
        user = (
            f"TABLE: {dataset_urn}\n"
            f"SCHEMA: {as_text(schema, 2500)}\n\n"
            f"LINEAGE (column-level where available):\n{lineage_text}\n\n"
            + (f"{upstream_text}\n\n" if upstream_text else "")
            + f"UNDOCUMENTED COLUMNS ({len(targets)}): {', '.join(targets)}"
        )

        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=1500)
        proposals = {
            p.get("column"): p
            for p in parsed.get("proposals", [])
            if isinstance(p, dict)
        }

        claims: list[Claim] = []
        now = time.time()
        for column in targets:
            p = proposals.get(column)
            if p is None or not str(p.get("description", "")).strip():
                continue  # nothing proposed means nothing to claim
            claims.append(
                Claim(
                    agent_id=self.agent_id,
                    model_id=model_id or self.llm.model,
                    claim_type=ENRICHMENT,
                    entity_urn=dataset_urn,
                    prediction={
                        "kind": "column_doc",
                        "column": column,
                        "description": str(p["description"])[:500],
                    },
                    confidence=clamp_confidence(p.get("confidence"), 0.05, 0.95),
                    evidence=[dataset_urn],
                    created_ts=now,
                )
            )
        return claims

    # -- the real work: write an accepted proposal into the catalog ---------

    def apply(self, claim: Claim) -> None:
        self.mcp.call(
            "update_description",
            {
                "entity_urn": claim.entity_urn,
                "column_path": claim.prediction["column"],
                "description": claim.prediction["description"],
                "operation": "replace",
            },
        )

    def unapply(self, claim: Claim) -> None:
        self.mcp.call(
            "update_description",
            {
                "entity_urn": claim.entity_urn,
                "column_path": claim.prediction["column"],
                "operation": "remove",
            },
        )
