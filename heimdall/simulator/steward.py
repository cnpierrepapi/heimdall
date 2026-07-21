"""Steward judgment for governance proposals, derived from world truth.

The simulator's steward is the same kind of keyword-and-truth referee the
column enricher already faces: nothing here is hand-labelled per claim. Each
evaluator maps one proposal kind to an accept/revert verdict by consulting
the world model, so settlement stays derivable and reproducible.

All governance proposals share the ENRICHMENT claim type: they are all
"propose an artifact, a steward accepts or reverts it" claims, and they all
share one luck baseline, the pooled acceptance rate for machine-written
metadata across every proposing agent. Beating that pool, not a coin flip,
is what earns a skilled verdict.
"""

from __future__ import annotations

from ..claims import Claim
from .world import World

# prediction["kind"] values for governance proposals; a missing kind means a
# plain column documentation proposal (the original enricher shape).
KIND_COLUMN_DOC = "column_doc"
KIND_TABLE_DOC = "table_doc"
KIND_PII = "pii"
KIND_OWNER = "owner"
KIND_DOMAIN = "domain"
KIND_TERM = "term"


def evaluate(world: World, claim: Claim) -> bool:
    """True = the steward accepts the proposal, False = reverts it."""
    dataset = world.by_urn(claim.entity_urn).name
    pred = claim.prediction
    kind = pred.get("kind", KIND_COLUMN_DOC)

    if kind in (KIND_COLUMN_DOC,):
        col = world.column(dataset, str(pred.get("column", "")))
        if col is None:
            return False
        text = str(pred.get("description", "")).lower()
        return bool(col.gold_keywords) and any(
            k in text for k in col.gold_keywords
        )

    if kind == KIND_TABLE_DOC:
        return world.accepts_table_description(
            dataset, str(pred.get("description", ""))
        )

    if kind == KIND_PII:
        truth = world.pii_type(dataset, str(pred.get("column", "")))
        # The steward accepts a PII flag when the column really is PII; the
        # specific type must match truth (an email tagged as a name is wrong).
        return truth is not None and truth == pred.get("pii_type")

    if kind == KIND_OWNER:
        return world.datasets[dataset].owner == pred.get("owner")

    if kind == KIND_DOMAIN:
        return world.datasets[dataset].domain == pred.get("domain")

    if kind == KIND_TERM:
        truth = world.term_for(dataset, str(pred.get("column", "")))
        return truth is not None and truth == pred.get("term")

    raise ValueError(f"unknown governance proposal kind: {kind}")
