"""Writeback layer: ledger-to-catalog projection logic, exercised with fakes."""

from __future__ import annotations

import time

import pytest

from heimdall.claims import BLAST_RADIUS, ENRICHMENT, Claim, ClaimStore
from heimdall.skill import HARMFUL, LUCK, SKILLED, UNSETTLED
from heimdall.writeback import (
    PROP_TRUST,
    TAG_HARMFUL,
    TAG_SKILLED,
    TAG_UNPROVEN,
    annotate_authored_datasets,
    apply_accepted_enrichments,
    dossier_markdown,
    ensure_tags,
    verdict_tag,
)

URN_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
URN_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"


class FakeMCP:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return {"success": True}


class FakeEmitter:
    def __init__(self):
        self.emitted = []

    def emit(self, mcpw):
        self.emitted.append(mcpw)


def _claim(claim_type, urn, agent="agent-x", **prediction):
    return Claim(
        agent_id=agent,
        model_id="m",
        claim_type=claim_type,
        entity_urn=urn,
        prediction=prediction,
        confidence=0.8,
        created_ts=time.time() - 10,
    )


def test_verdict_tag_mapping():
    assert verdict_tag(SKILLED) == TAG_SKILLED
    assert verdict_tag(HARMFUL) == TAG_HARMFUL
    assert verdict_tag(LUCK) == TAG_UNPROVEN
    assert verdict_tag(UNSETTLED) == TAG_UNPROVEN


def test_ensure_tags_emits_all_three():
    emitter = FakeEmitter()
    urns = ensure_tags(emitter)
    assert len(emitter.emitted) == 3
    assert set(urns) == {
        f"urn:li:tag:{t}" for t in (TAG_SKILLED, TAG_UNPROVEN, TAG_HARMFUL)
    }


def test_apply_accepted_enrichments_filters(tmp_path):
    store = ClaimStore(str(tmp_path / "l.db"))
    accepted = store.record(
        _claim(ENRICHMENT, URN_A, column="c1", description="Amount in USD.")
    )
    reverted = store.record(
        _claim(ENRICHMENT, URN_B, column="c2", description="Filler text.")
    )
    unsettled = store.record(
        _claim(ENRICHMENT, URN_B, column="c3", description="Never reviewed.")
    )
    blast = store.record(_claim(BLAST_RADIUS, URN_A, will_break=True))
    store.settle(accepted.claim_id, outcome={}, correct=True)
    store.settle(reverted.claim_id, outcome={}, correct=False)
    store.settle(blast.claim_id, outcome={}, correct=True)

    mcp = FakeMCP()
    authored = apply_accepted_enrichments(mcp, store)
    assert authored == {URN_A: "agent-x"}
    assert len(mcp.calls) == 1
    tool, args = mcp.calls[0]
    assert tool == "update_description"
    assert args["column_path"] == "c1"
    store.close()


def test_annotate_stamps_tag_and_properties():
    mcp, emitter = FakeMCP(), FakeEmitter()
    report = {
        "agent-x": {"verdict": SKILLED, "trust": 71.2},
        "agent-y": {"verdict": LUCK, "trust": 52.0},
    }
    stamped = annotate_authored_datasets(
        mcp, emitter, {URN_A: "agent-x", URN_B: "agent-y"}, report
    )
    assert stamped == {URN_A: TAG_SKILLED, URN_B: TAG_UNPROVEN}
    tags_calls = [c for c in mcp.calls if c[0] == "add_tags"]
    assert tags_calls[0][1]["entity_urns"] == [URN_A]
    assert tags_calls[0][1]["tag_urns"] == [f"urn:li:tag:{TAG_SKILLED}"]
    # one structured-properties aspect per dataset
    assert len(emitter.emitted) == 2
    props = emitter.emitted[0].aspect.properties
    trust_values = [p.values for p in props if p.propertyUrn == PROP_TRUST]
    assert trust_values == [[71.2]]


def test_annotate_skips_unknown_agents():
    mcp, emitter = FakeMCP(), FakeEmitter()
    stamped = annotate_authored_datasets(mcp, emitter, {URN_A: "ghost"}, {})
    assert stamped == {}
    assert mcp.calls == []


def test_dossier_markdown_content_and_copy_rules():
    rec = {
        "verdict": SKILLED,
        "trust": 66.1,
        "n_total": 12,
        "n_settled": 10,
        "win_rate": 0.9,
        "brier_mean": 0.05,
        "ece": 0.04,
        "expected_null_wins": 5.2,
        "p_value": 0.002,
        "q_value": 0.008,
        "calibration": [
            {"bin_low": 0.8, "bin_high": 0.9, "n": 6, "mean_confidence": 0.85, "frac_true": 0.83}
        ],
    }
    settled = [_claim(ENRICHMENT, URN_A, column="c", description="d")]
    settled[0].settled_ts = time.time()
    settled[0].correct = True
    text = dossier_markdown("agent-x", rec, settled)
    assert "Agent trust dossier: agent-x" in text
    assert "skilled" in text
    assert "66.1/100" in text
    assert "| 0.8 to 0.9 | 6 | 0.83 |" in text
    assert "RIGHT (enrichment" in text
    assert "—" not in text  # no em dashes in shipped copy
