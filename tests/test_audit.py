"""Observability-to-catalog audit writeback."""

from __future__ import annotations

from heimdall.audit import (
    audit_dossier_markdown,
    stamp_provenance,
    touched_assets,
)
from heimdall.grounding import Finding, SEV_HARMFUL
from heimdall.observability import EventStore, ObservationEvent
from heimdall.simulator.steward import KIND_COLUMN_DOC
from heimdall.writeback import TAG_HARMFUL, TAG_SKILLED, tag_urn

DS_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_orders,PROD)"
DS_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_customers,PROD)"


def wev(agent, urn, op="write", status="ok", tool="update_description", operation="replace"):
    return ObservationEvent(agent_id=agent, tool=tool, op=op, status=status,
                            args={"entity_urn": urn, "column_path": "c",
                                  "description": "d", "operation": operation})


class FakeMCP:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return {}


class FakeEmitter:
    def __init__(self):
        self.emitted = []

    def emit(self, mcp):
        self.emitted.append(mcp)


def test_touched_assets_latest_writer_wins(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    store.record(wev("expert", DS_A))
    store.record(wev("rogue", DS_B))
    store.record(wev("expert", DS_A))  # latest for DS_A still expert
    store.record(wev("skip", DS_A, status="error"))  # not ok -> ignored
    store.record(wev("skip", DS_A, operation="remove"))  # removal -> ignored
    store.record(wev("reader", DS_A, op="read", tool="get_entities"))  # read -> ignored
    touched = touched_assets(store)
    assert touched[DS_A] == ("expert", KIND_COLUMN_DOC)
    assert touched[DS_B] == ("rogue", KIND_COLUMN_DOC)


def test_stamp_provenance_tags_and_props():
    mcp, emitter = FakeMCP(), FakeEmitter()
    touched = {DS_A: ("expert", KIND_COLUMN_DOC), DS_B: ("rogue", KIND_COLUMN_DOC)}
    report = {
        "expert": {KIND_COLUMN_DOC: {"trust": 66.0, "verdict": "skilled", "n_settled": 6}},
        "rogue": {KIND_COLUMN_DOC: {"trust": 40.0, "verdict": "worse than chance", "n_settled": 6}},
    }
    stamped = stamp_provenance(mcp, emitter, touched, report)
    # each dataset got an add_tags call with the right provenance tag
    tagged = {args["entity_urns"][0]: args["tag_urns"][0]
              for tool, args in mcp.calls if tool == "add_tags"}
    assert tagged[DS_A] == tag_urn(TAG_SKILLED)
    assert tagged[DS_B] == tag_urn(TAG_HARMFUL)
    # each dataset got a structured-properties MCP emitted
    assert len(emitter.emitted) == 2
    assert stamped[DS_A]["verdict"] == "skilled"
    assert stamped[DS_B]["tag"] == TAG_HARMFUL


def test_dossier_lists_trust_and_findings():
    kinds = {KIND_COLUMN_DOC: {"trust": 40.0, "verdict": "worse than chance", "n_settled": 6}}
    findings = [Finding(agent_id="rogue", check_type="glossary_conflict",
                        severity=SEV_HARMFUL, column="amount_usd",
                        reason="asserts the wrong glossary term")]
    activity = {"total": 6, "reads": 0, "writes": 6, "errors": 0, "held": 0, "blocked": 0}
    md = audit_dossier_markdown("rogue", kinds, findings, activity)
    assert "Agent audit dossier: rogue" in md
    assert "Trust by work kind" in md
    assert "worse than chance" in md
    assert "glossary_conflict" in md and "amount_usd" in md
