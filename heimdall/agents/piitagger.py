"""PII tagger: flag columns that hold personal data.

Reads a table's schema and existing documentation and flags the columns that
contain personally identifiable information, typed from a fixed vocabulary.
Flagging is claim-per-column with abstention: the agent only claims columns
it believes are PII, and every flag is falsifiable against the steward's
review.

The stakes are asymmetric by design: a confident flag on a pseudonymous key
or a demographic column (classic false positives) settles wrong and costs
Brier score, which is exactly the accountability a compliance workflow needs
from a machine tagger.
"""

from __future__ import annotations

import time

from ..claims import ENRICHMENT, Claim
from ..llm import LLMClient
from ..mcp_client import DataHubMCP
from .common import as_text, clamp_confidence

PII_TYPES = ("email", "person_name", "phone", "address", "national_id")

_SYSTEM = """You are a data protection reviewer classifying warehouse columns for PII.

You get a table's schema with column names, types, and documentation. Flag ONLY the columns that directly contain personally identifiable information, and type each flag from this vocabulary: email, person_name, phone, address, national_id.

Rules a careful reviewer follows:
- Pseudonymous surrogate keys (customer_id, user_id) are NOT PII by themselves.
- Coarse demographics (country, region) are NOT PII by themselves.
- Timestamps and amounts are NOT PII.
- Only flag a column when its content itself identifies a person.

Reply with ONLY a JSON object, no prose:
{"flags": [{"column": "...", "pii_type": "...", "confidence": 0.05-0.95}]}

An empty flags list is a valid answer. Confidence is your probability that a data steward confirms the flag."""


class PiiTaggerAgent:
    agent_id = "pii-tagger"

    def __init__(self, mcp: DataHubMCP, llm: LLMClient, agent_id: str = "pii-tagger"):
        self.mcp = mcp
        self.llm = llm
        self.agent_id = agent_id

    def propose(self, dataset_urn: str, model_id: str = "") -> list[Claim]:
        schema = self.mcp.list_schema_fields(dataset_urn)
        known = {
            f.get("fieldPath")
            for f in (schema.get("fields", []) if isinstance(schema, dict) else [])
        }
        user = f"TABLE: {dataset_urn}\nSCHEMA: {as_text(schema, 2500)}"
        parsed = self.llm.chat_json(_SYSTEM, user, max_tokens=800)

        claims: list[Claim] = []
        now = time.time()
        for flag in parsed.get("flags", []):
            if not isinstance(flag, dict):
                continue
            column = flag.get("column")
            pii_type = flag.get("pii_type")
            if column not in known or pii_type not in PII_TYPES:
                continue  # hallucinated column or type: nothing to claim
            claims.append(
                Claim(
                    agent_id=self.agent_id,
                    model_id=model_id or self.llm.model,
                    claim_type=ENRICHMENT,
                    entity_urn=dataset_urn,
                    prediction={
                        "kind": "pii",
                        "column": column,
                        "pii_type": pii_type,
                    },
                    confidence=clamp_confidence(flag.get("confidence"), 0.05, 0.95),
                    evidence=[dataset_urn],
                    created_ts=now,
                )
            )
        return claims
