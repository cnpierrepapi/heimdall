"""Rogue-writer auditor: detect catalog edits that bypassed the ledger.

Deterministic, no model. The ledger records every accepted proposal an
instrumented agent made; the catalog shows what is actually there. When an
accepted description no longer appears on the asset, somebody wrote around
the gateway. The auditor claims tampered or clean per audited dataset, with
confidence scaled by the strength of the diff.

The claim is falsifiable because the auditor only sees the coarse entity
read at detection time; settlement is the definitive account of what a rogue
session actually touched (in the demo, the orchestrator's own tamper list;
in production, a provenance sweep).
"""

from __future__ import annotations

import json
import time
from typing import Optional

from ..claims import ENRICHMENT, TAMPER_AUDIT, Claim, ClaimStore
from ..mcp_client import DataHubMCP


def accepted_texts(store: ClaimStore, entity_urn: str) -> list[str]:
    """Latest accepted description per target (table plus each column)."""
    latest: dict[tuple, tuple[float, str]] = {}
    for c in store.claims(claim_type=ENRICHMENT, entity_urn=entity_urn, settled=True):
        if not c.correct:
            continue
        text = c.prediction.get("description")
        if not text:
            continue  # tags, owners, domains, terms have no text to diff
        key = (c.prediction.get("kind", "column_doc"), c.prediction.get("column"))
        if key not in latest or c.settled_ts > latest[key][0]:
            latest[key] = (c.settled_ts or 0.0, str(text))
    return [text for _, text in latest.values()]


class RogueAuditorAgent:
    agent_id = "rogue-auditor"
    model_id = "deterministic/ledger-diff"

    def __init__(self, mcp: DataHubMCP, agent_id: str = "rogue-auditor"):
        self.mcp = mcp
        self.agent_id = agent_id

    def audit(self, store: ClaimStore, entity_urn: str) -> Optional[Claim]:
        expected = accepted_texts(store, entity_urn)
        if not expected:
            return None  # nothing on the ledger to audit against

        blob = self.mcp.get_entities([entity_urn])
        catalog = blob if isinstance(blob, str) else json.dumps(blob)
        missing = [t for t in expected if t not in catalog]

        tampered = bool(missing)
        confidence = (
            min(0.6 + 0.1 * len(missing), 0.95) if tampered else 0.85
        )
        return Claim(
            agent_id=self.agent_id,
            model_id=self.model_id,
            claim_type=TAMPER_AUDIT,
            entity_urn=entity_urn,
            prediction={
                "kind": "tamper_audit",
                "tampered": tampered,
                "missing": [m[:120] for m in missing[:5]],
                "n_expected": len(expected),
            },
            confidence=confidence,
            evidence=[entity_urn],
            created_ts=time.time(),
        )
