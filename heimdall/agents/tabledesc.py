"""Table describer: propose dataset-level documentation.

The column enricher's sibling one level up: for a dataset with no table
description, gather the schema, the immediate lineage neighborhood, and any
column docs that exist, then draft a one-paragraph summary of what the table
is in a single judgment call.

Each proposal is an ENRICHMENT claim (kind=table_doc) whose confidence is
P(a steward accepts the text). Accepted proposals are written back to the
catalog as the dataset's description.
"""

from __future__ import annotations

import time

from ..claims import ENRICHMENT, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from .common import as_text, clamp_confidence, extract_dataset_urns

_SYSTEM = """You are a data steward writing the table-level description for a warehouse catalog entry.

You get the table's schema (with any column documentation), its direct upstream and downstream tables, and the lineage between them. Write ONE short paragraph (1-3 sentences) describing what this table is: the entity or process it records, the warehouse layer it sits in (raw feed, staging, mart, reporting), and what it is built from or feeds when lineage makes that clear. Be specific and factual; no filler like "this table stores data". Plain prose, no em dashes.

Reply with ONLY a JSON object, no prose:
{"description": "...", "confidence": 0.05-0.95}

Confidence is your probability that a human steward would ACCEPT your description as accurate."""


class TableDescriberAgent:
    agent_id = "table-describer"

    def __init__(
        self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "table-describer"
    ):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    # -- evidence gathering (fixed steps, no model involvement) -------------

    def neighborhood(self, urn: str) -> str:
        upstream = self.mcp.get_lineage(urn, upstream=True, max_hops=1)
        downstream = self.mcp.get_lineage(urn, upstream=False, max_hops=1)
        ups = extract_dataset_urns(upstream, exclude=[urn])
        downs = extract_dataset_urns(downstream, exclude=[urn])
        return (
            f"UPSTREAM TABLES: {', '.join(ups) or '(none, this is a source feed)'}\n"
            f"DOWNSTREAM TABLES: {', '.join(downs) or '(none)'}"
        )

    # -- the one judgment call ----------------------------------------------

    def propose(self, dataset_urn: str, model_id: str = "") -> list[Claim]:
        schema = self.mcp.list_schema_fields(dataset_urn)
        user = (
            f"TABLE: {dataset_urn}\n"
            f"SCHEMA: {as_text(schema, 2500)}\n\n"
            f"{self.neighborhood(dataset_urn)}"
        )
        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=600)
        description = str(parsed.get("description", "")).strip()
        if not description:
            return []
        return [
            Claim(
                agent_id=self.agent_id,
                model_id=model_id or self.llm.model,
                claim_type=ENRICHMENT,
                entity_urn=dataset_urn,
                prediction={
                    "kind": "table_doc",
                    "description": description[:800],
                },
                confidence=clamp_confidence(parsed.get("confidence"), 0.05, 0.95),
                evidence=[dataset_urn],
                created_ts=time.time(),
            )
        ]

    # -- the real work: write an accepted proposal into the catalog ---------

    def apply(self, claim: Claim) -> None:
        self.mcp.call(
            "update_description",
            {
                "entity_urn": claim.entity_urn,
                "description": claim.prediction["description"],
                "operation": "replace",
            },
        )
