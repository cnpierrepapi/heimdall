"""Glossary term mapper: link columns to business glossary terms.

Given a table and the glossary, the agent maps columns to the terms they
mean, with abstention: a column with no matching term gets no claim. The
traps are semantic near-misses (cash settled is not recognized revenue, an
event count is not active customers), so name-similarity mapping loses to
actual reading of the docs and lineage.
"""

from __future__ import annotations

import time

from ..claims import ENRICHMENT, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from .common import as_text, clamp_confidence

_SYSTEM = """You are linking warehouse columns to business glossary terms.

You get a table's schema (names, types, documentation) and the glossary term list. Map each column that MEANS one of the terms to that term. Map on meaning, not name similarity: a cash-settled payment amount is not recognized revenue; a raw event count is not a count of active customers. Columns with no matching term must be omitted.

Reply with ONLY a JSON object, no prose:
{"mappings": [{"column": "...", "term": "...", "confidence": 0.05-0.95}]}

An empty mappings list is a valid answer. Terms MUST come from the list verbatim. Confidence is your probability that a steward accepts the link."""


class TermMapperAgent:
    agent_id = "term-mapper"

    def __init__(self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "term-mapper"):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    def propose(
        self, dataset_urn: str, terms: list[str], model_id: str = ""
    ) -> list[Claim]:
        if not terms:
            return []
        schema = self.mcp.list_schema_fields(dataset_urn)
        known = {
            f.get("fieldPath")
            for f in (schema.get("fields", []) if isinstance(schema, dict) else [])
        }
        user = (
            f"TABLE: {dataset_urn}\n"
            f"SCHEMA: {as_text(schema, 2200)}\n\n"
            f"GLOSSARY TERMS ({len(terms)}): {', '.join(terms)}"
        )
        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=800)

        claims: list[Claim] = []
        now = time.time()
        seen: set[str] = set()
        for m in parsed.get("mappings", []):
            if not isinstance(m, dict):
                continue
            column = m.get("column")
            term = m.get("term")
            if column not in known or term not in terms or column in seen:
                continue
            seen.add(str(column))
            claims.append(
                Claim(
                    agent_id=self.agent_id,
                    model_id=model_id or self.llm.model,
                    claim_type=ENRICHMENT,
                    entity_urn=dataset_urn,
                    prediction={"kind": "term", "column": column, "term": term},
                    confidence=clamp_confidence(m.get("confidence"), 0.05, 0.95),
                    evidence=[dataset_urn],
                    created_ts=now,
                )
            )
        return claims
