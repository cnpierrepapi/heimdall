"""T5: writer-agent selection and the catalog write dispatch (offline)."""

from __future__ import annotations

import pytest

from heimdall.agentrun import build_agent
from heimdall.agents.enricher import EnricherAgent
from heimdall.agents.piitagger import PiiTaggerAgent
from heimdall.claims import ENRICHMENT, Claim
from heimdall.governance import apply_claim, pii_tag_urn
from heimdall.roster import RosterAgent


class FakeMCP:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return {}


def _claim(kind, **pred):
    return Claim(
        agent_id="a", model_id="m", claim_type=ENRICHMENT,
        entity_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,c.raw_x,PROD)",
        prediction={"kind": kind, **pred}, confidence=0.7, evidence=[], created_ts=0.0,
    )


def test_build_agent_maps_kind_to_writer():
    doc = build_agent(RosterAgent("atlas-doc", "column_doc", "diligent"), None, None)
    pii = build_agent(RosterAgent("vega-pii", "pii", "diligent"), None, None)
    assert isinstance(doc, EnricherAgent) and doc.agent_id == "atlas-doc"
    assert isinstance(pii, PiiTaggerAgent) and pii.agent_id == "vega-pii"
    # the profile prompt is wired into the writer
    assert "meticulous" in doc.system


def test_build_agent_rejects_unwired_kind():
    with pytest.raises(ValueError):
        build_agent(RosterAgent("x", "owner", "diligent"), None, None)


def test_apply_column_doc_writes_description_with_column():
    mcp = FakeMCP()
    apply_claim(mcp, _claim("column_doc", column="fare_usd", description="Trip fare in USD."))
    tool, args = mcp.calls[0]
    assert tool == "update_description"
    assert args["column_path"] == "fare_usd"
    assert args["operation"] == "replace"


def test_apply_pii_writes_pii_tag():
    mcp = FakeMCP()
    apply_claim(mcp, _claim("pii", column="email", pii_type="email"))
    tool, args = mcp.calls[0]
    assert tool == "add_tags"
    assert args["tag_urns"] == [pii_tag_urn("email")]
    assert args["column_paths"] == ["email"]


def test_apply_rejects_unknown_kind():
    with pytest.raises(ValueError):
        apply_claim(FakeMCP(), _claim("owner", owner="team"))
