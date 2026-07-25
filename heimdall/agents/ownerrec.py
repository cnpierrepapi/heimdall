"""Owner recommender: propose an owning team for a dataset.

Given a dataset and the candidate teams, the agent reads the naming, layer, and
lineage neighborhood (who owns the neighbors is real signal: staging tables
usually belong to the team that owns the mart they feed) and picks one team in a
single judgment call.

Nothing this agent proposes earns it trust. A catalog does not know who owns it,
so the answer was assigned rather than derived, and grading guesses against it
would score luck. Heimdall says so explicitly instead of quietly scoring it.

The agent runs anyway, and that is the point. Its actions are observed, and a
proposal that contradicts the catalog is caught and cited in flight, exactly like
any other write. A finding is a statement about one action's consistency with the
recorded catalog, which is true whether or not the recorded answer was derivable.
A trust score is a claim about the agent's ability, which is not.

The candidate teams come from the catalog, so the pick is one of n and the
refusal of an off-list answer is a real behaviour rather than a parsing accident.
"""

from __future__ import annotations

import time

from ..claims import ENRICHMENT, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from .common import as_text, clamp_confidence, extract_dataset_urns

_SYSTEM = """You are assigning ownership for datasets in a warehouse catalog.

You get one dataset (schema, docs, upstream and downstream neighbors) and a list of candidate teams. Pick the SINGLE team that should own this dataset. Reason from warehouse convention: raw source feeds belong to the platform/ingestion team; staging tables and marts belong to the analytics team of their subject area; when a table blends areas, ownership follows its primary business purpose.

Reply with ONLY a JSON object, no prose:
{"owner": "...", "confidence": 0.05-0.95}

The owner MUST be one of the candidates verbatim. Confidence is your probability that the assignment is accepted."""


class OwnerRecommenderAgent:
    agent_id = "owner-recommender"

    def __init__(
        self,
        mcp: DataHubMCP,
        llm: LLMClient,
        agent_id: str = "owner-recommender",
        system: str = _SYSTEM,
        teams: tuple[str, ...] = (),
    ):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id
        self.system = system
        self.teams = tuple(teams)

    def neighborhood(self, urn: str) -> str:
        upstream = self.mcp.get_lineage(urn, upstream=True, max_hops=1)
        downstream = self.mcp.get_lineage(urn, upstream=False, max_hops=1)
        ups = extract_dataset_urns(upstream, exclude=[urn])
        downs = extract_dataset_urns(downstream, exclude=[urn])
        return (
            f"UPSTREAM: {', '.join(ups) or '(none, source feed)'}\n"
            f"DOWNSTREAM: {', '.join(downs) or '(none)'}"
        )

    def propose(self, dataset_urn: str, model_id: str = "") -> list[Claim]:
        teams = self.teams
        if len(teams) < 2:
            return []  # a one-candidate pick proves nothing
        schema = self.mcp.list_schema_fields(dataset_urn)
        user = (
            f"DATASET: {dataset_urn}\n"
            f"SCHEMA: {as_text(schema, 1800)}\n"
            f"{self.neighborhood(dataset_urn)}\n\n"
            f"CANDIDATE TEAMS ({len(teams)}): {', '.join(teams)}"
        )
        parsed = self.llm.chat_json(self.system, user, max_tokens=300)
        owner = parsed.get("owner")
        if owner not in teams:
            return []  # refusing an off-list answer beats recording noise
        return [
            Claim(
                agent_id=self.agent_id,
                model_id=model_id or self.llm.model,
                claim_type=ENRICHMENT,
                entity_urn=dataset_urn,
                prediction={
                    "kind": "owner",
                    "owner": owner,
                    "n_candidates": len(teams),
                },
                confidence=clamp_confidence(parsed.get("confidence"), 0.05, 0.95),
                evidence=[dataset_urn],
                created_ts=time.time(),
            )
        ]
