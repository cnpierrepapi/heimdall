"""Domain assigner: file a dataset under a business domain.

Same scaffold as the owner recommender with a different judgment target:
given the candidate domains, pick where the dataset belongs. The interesting
cases are blended assets (a KPI report reading both revenue and engagement
inputs), where "a bit of both" is not an option and the agent has to commit
to the primary purpose.
"""

from __future__ import annotations

import time

from ..claims import ENRICHMENT, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from .common import as_text, clamp_confidence, extract_dataset_urns

_SYSTEM = """You are organizing a warehouse catalog into business domains.

You get one dataset (schema, docs, upstream and downstream neighbors) and the list of candidate domains. Pick the SINGLE domain this dataset belongs to. When a dataset blends inputs from several areas, file it under its primary business purpose, the thing its consumers use it for.

Reply with ONLY a JSON object, no prose:
{"domain": "...", "confidence": 0.05-0.95}

The domain MUST be one of the candidates verbatim. Confidence is your probability that the assignment is accepted."""


class DomainAssignerAgent:
    agent_id = "domain-assigner"

    def __init__(
        self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "domain-assigner"
    ):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    def neighborhood(self, urn: str) -> str:
        upstream = self.mcp.get_lineage(urn, upstream=True, max_hops=1)
        downstream = self.mcp.get_lineage(urn, upstream=False, max_hops=1)
        ups = extract_dataset_urns(upstream, exclude=[urn])
        downs = extract_dataset_urns(downstream, exclude=[urn])
        return (
            f"UPSTREAM: {', '.join(ups) or '(none, source feed)'}\n"
            f"DOWNSTREAM: {', '.join(downs) or '(none)'}"
        )

    def propose(
        self, dataset_urn: str, domains: list[str], model_id: str = ""
    ) -> list[Claim]:
        if len(domains) < 2:
            return []
        schema = self.mcp.list_schema_fields(dataset_urn)
        user = (
            f"DATASET: {dataset_urn}\n"
            f"SCHEMA: {as_text(schema, 1800)}\n"
            f"{self.neighborhood(dataset_urn)}\n\n"
            f"CANDIDATE DOMAINS ({len(domains)}): {', '.join(domains)}"
        )
        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=300)
        domain = parsed.get("domain")
        if domain not in domains:
            return []
        return [
            Claim(
                agent_id=self.agent_id,
                model_id=model_id or self.llm.model,
                claim_type=ENRICHMENT,
                entity_urn=dataset_urn,
                prediction={
                    "kind": "domain",
                    "domain": domain,
                    "n_candidates": len(domains),
                },
                confidence=clamp_confidence(parsed.get("confidence"), 0.05, 0.95),
                evidence=[dataset_urn],
                created_ts=time.time(),
            )
        ]
