"""Naive governance baseline: the heuristic rival the pool needs.

A deliberately unsophisticated proposer that does what quick-and-dirty
metadata scripts actually do: pattern-match on column names, file everything
under the biggest domain, hand every table to the busiest team, and write
filler documentation. No model, fully deterministic, always confident.

It exists for two honest reasons. First, the skill test's luck baseline for
proposals is the pooled acceptance rate across agents; with only excellent
proposers in the pool, nobody can be distinguished from the pool they define
(gotcha G-19). A realistic weak rival restores contrast. Second, it is the
market's actual null hypothesis: regex taggers and naming-convention scripts
are what teams use today, and the settled ledger showing exactly where they
fail is the product's argument.
"""

from __future__ import annotations

import time

from ..claims import ENRICHMENT, Claim
from ..mcp_client import DataHubMCP
from .common import as_text


class NaiveGovernanceAgent:
    """Scripted heuristics, one claim stream, no LLM."""

    agent_id = "naive-governance"

    def __init__(self, mcp: DataHubMCP, agent_id: str = "naive-governance"):
        self.mcp = mcp
        self.agent_id = agent_id
        self.model_id = "heuristics/none"

    def _columns(self, dataset_urn: str) -> list[str]:
        schema = self.mcp.list_schema_fields(dataset_urn)
        return [
            f["fieldPath"]
            for f in (schema.get("fields", []) if isinstance(schema, dict) else [])
            if f.get("fieldPath")
        ]

    def propose_all(
        self,
        dataset_urn: str,
        teams: list[str],
        domains: list[str],
        terms: list[str],
    ) -> list[Claim]:
        columns = self._columns(dataset_urn)
        now = time.time()

        def claim(prediction: dict, confidence: float) -> Claim:
            return Claim(
                agent_id=self.agent_id,
                model_id=self.model_id,
                claim_type=ENRICHMENT,
                entity_urn=dataset_urn,
                prediction=prediction,
                confidence=confidence,
                evidence=[dataset_urn],
                created_ts=now,
            )

        claims: list[Claim] = [
            # filler documentation, the classic
            claim(
                {
                    "kind": "table_doc",
                    "description": "This table stores business data for analytics use.",
                },
                0.9,
            ),
            # everything goes to the first team and the first domain
            claim(
                {"kind": "owner", "owner": sorted(teams)[0], "n_candidates": len(teams)},
                0.85,
            ),
            claim(
                {
                    "kind": "domain",
                    "domain": sorted(domains)[0],
                    "n_candidates": len(domains),
                },
                0.85,
            ),
        ]

        # name-pattern PII flagging: ids and demographics get flagged too
        for col in columns:
            low = col.lower()
            if "email" in low:
                claims.append(
                    claim({"kind": "pii", "column": col, "pii_type": "email"}, 0.9)
                )
            elif "name" in low:
                claims.append(
                    claim({"kind": "pii", "column": col, "pii_type": "person_name"}, 0.85)
                )
            elif low.endswith("_id"):
                claims.append(
                    claim({"kind": "pii", "column": col, "pii_type": "national_id"}, 0.8)
                )
            elif "country" in low:
                claims.append(
                    claim({"kind": "pii", "column": col, "pii_type": "address"}, 0.8)
                )

        # substring term mapping: "usd means revenue" and friends
        by_substring = [
            ("revenue", "Recognized Revenue"),
            ("usd", "Recognized Revenue"),
            ("amount", "Recognized Revenue"),
            ("country", "Customer Country"),
            ("customer", "Active Customers"),
        ]
        for col in columns:
            low = col.lower()
            for needle, term in by_substring:
                if needle in low and term in terms:
                    claims.append(
                        claim({"kind": "term", "column": col, "term": term}, 0.8)
                    )
                    break

        return claims
