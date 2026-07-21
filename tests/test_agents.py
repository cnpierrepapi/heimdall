"""Claim-shaping behavior of the LLM agents, exercised with fakes.

The LLM and MCP server are replaced with canned doubles; what is under test
is everything around the judgment call: evidence extraction, directional
normalization, confidence clamps, abstentions for omitted answers, and the
uniform-prior fallback for invalid root-cause picks. These invariants are
what make settled records comparable across agents, so they get tests even
though the agents themselves are exercised live.
"""

from __future__ import annotations

import pytest

from heimdall.agents import (
    BlastRadiusAgent,
    EnricherAgent,
    FreshnessSentinelAgent,
    TriageAgent,
)
from heimdall.agents.common import clamp_confidence, extract_dataset_urns
from heimdall.agents.enricher import undocumented_columns
from heimdall.claims import BLAST_RADIUS, ENRICHMENT, FRESHNESS_SLA, ROOT_CAUSE

URN_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
URN_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"
URN_C = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.c,PROD)"


class FakeLLM:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def chat_json(self, system, user, max_tokens=2000, retries=2):
        self.prompts.append(user)
        return self.responses.pop(0)


class FakeMCP:
    def __init__(self, lineage=None, schemas=None, entities=None):
        self.lineage = lineage or {}
        self.schemas = schemas or {}
        self.entities = entities or {}
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return {"success": True}

    def get_lineage(self, urn, upstream, max_hops=3):
        return self.lineage.get((urn, upstream), {"entities": []})

    def list_schema_fields(self, urn):
        return self.schemas.get(urn, {"urn": urn, "fields": []})

    def get_entities(self, urns):
        return {"entities": [self.entities.get(u, {"urn": u}) for u in urns]}


# -- common helpers ----------------------------------------------------------


def test_extract_dataset_urns_dedupes_and_excludes():
    payload = {"entities": [{"urn": URN_A}, {"urn": URN_B}, {"urn": URN_A}]}
    assert extract_dataset_urns(payload, exclude=[URN_A]) == [URN_B]


def test_clamp_confidence_handles_garbage():
    assert clamp_confidence("not a number", 0.5, 0.95, default=0.6) == 0.6
    assert clamp_confidence(1.7, 0.5, 0.95) == 0.95
    assert clamp_confidence(0.1, 0.5, 0.95) == 0.5


def test_undocumented_columns_missing_and_blank():
    schema = {
        "fields": [
            {"fieldPath": "a", "description": "documented"},
            {"fieldPath": "b"},
            {"fieldPath": "c", "description": "   "},
        ]
    }
    assert undocumented_columns(schema) == ["b", "c"]


# -- sentinel ----------------------------------------------------------------


def test_sentinel_normalizes_direction_and_clamps():
    # conf 0.2 on "no miss" means the agent actually believes "miss" at 0.8
    llm = FakeLLM([{"will_miss_sla": False, "confidence": 0.2, "reason": "trend"}])
    agent = FreshnessSentinelAgent(FakeMCP(), llm)
    claim = agent.forecast(URN_A, [False, True, True], day=3)
    assert claim.claim_type == FRESHNESS_SLA
    assert claim.prediction["will_miss_sla"] is True
    assert claim.confidence == pytest.approx(0.8)
    assert claim.prediction["day"] == 3


def test_sentinel_history_reaches_the_prompt():
    llm = FakeLLM([{"will_miss_sla": True, "confidence": 0.99}])
    agent = FreshnessSentinelAgent(FakeMCP(), llm)
    claim = agent.forecast(URN_A, [True, True], day=2)
    assert claim.confidence == 0.95  # clamped
    assert "day 0: LATE" in llm.prompts[0]
    assert "day 1: LATE" in llm.prompts[0]


# -- enricher ----------------------------------------------------------------


def _enricher_world():
    schemas = {
        URN_A: {
            "urn": URN_A,
            "fields": [
                {"fieldPath": "id", "description": "Primary key."},
                {"fieldPath": "amount_usd"},
                {"fieldPath": "code"},
            ],
        },
        URN_B: {"urn": URN_B, "fields": [{"fieldPath": "id", "description": "x"}]},
    }
    lineage = {(URN_A, True): {"entities": [{"urn": URN_B}]}}
    return FakeMCP(lineage=lineage, schemas=schemas)


def test_enricher_claims_only_real_proposals():
    llm = FakeLLM(
        [
            {
                "proposals": [
                    {"column": "amount_usd", "description": "Paid amount in USD.", "confidence": 0.8},
                    {"column": "bogus_col", "description": "Should be ignored.", "confidence": 0.9},
                    # "code" omitted: no claim should be recorded for it
                ]
            }
        ]
    )
    agent = EnricherAgent(_enricher_world(), llm)
    claims = agent.propose(URN_A)
    assert [c.prediction["column"] for c in claims] == ["amount_usd"]
    assert claims[0].claim_type == ENRICHMENT
    assert claims[0].confidence == pytest.approx(0.8)


def test_enricher_skips_fully_documented_tables():
    llm = FakeLLM([])  # must never be consulted
    agent = EnricherAgent(_enricher_world(), llm)
    assert agent.propose(URN_B) == []


def test_enricher_apply_calls_update_description():
    mcp = _enricher_world()
    llm = FakeLLM(
        [{"proposals": [{"column": "code", "description": "Discount code.", "confidence": 0.7}]}]
    )
    agent = EnricherAgent(mcp, llm)
    claim = agent.propose(URN_A)[0]
    agent.apply(claim)
    tool, args = mcp.calls[-1]
    assert tool == "update_description"
    assert args["entity_urn"] == URN_A
    assert args["column_path"] == "code"
    assert args["description"] == "Discount code."


# -- triage ------------------------------------------------------------------


def _triage_world():
    lineage = {(URN_A, True): {"entities": [{"urn": URN_B}, {"urn": URN_C}]}}
    return FakeMCP(lineage=lineage)


def test_triage_valid_pick():
    llm = FakeLLM([{"root_cause_urn": URN_C, "confidence": 0.75, "reason": "late feed"}])
    agent = TriageAgent(_triage_world(), llm)
    claim = agent.diagnose("urn:li:incident:x", URN_A, "stale", ["b: ON_TIME", "c: LATE"])
    assert claim.claim_type == ROOT_CAUSE
    assert claim.prediction["root_cause_urn"] == URN_C
    assert claim.prediction["n_candidates"] == 2
    assert claim.confidence == pytest.approx(0.75)


def test_triage_invalid_pick_falls_back_to_uniform_prior():
    llm = FakeLLM([{"root_cause_urn": "urn:li:dataset:(x,made.up,PROD)", "confidence": 0.9}])
    agent = TriageAgent(_triage_world(), llm)
    claim = agent.diagnose("urn:li:incident:x", URN_A, "stale", [])
    assert claim.prediction["root_cause_urn"] == URN_B
    assert claim.confidence == pytest.approx(0.5)  # 1/2 candidates
    assert "non-candidate" in claim.prediction["reason"]


def test_triage_no_candidates_returns_none():
    llm = FakeLLM([])
    agent = TriageAgent(FakeMCP(), llm)
    assert agent.diagnose("urn:li:incident:x", URN_A, "stale", []) is None


# -- blast radius (post-refactor guard) --------------------------------------


def test_blast_abstains_for_omitted_candidates():
    lineage = {(URN_A, False): {"entities": [{"urn": URN_B}, {"urn": URN_C}]}}
    schemas = {
        u: {"urn": u, "fields": [{"fieldPath": "id"}]} for u in (URN_A, URN_B, URN_C)
    }
    llm = FakeLLM(
        [
            {
                "assessments": [
                    {"dataset_urn": URN_B, "will_break": True, "confidence": 0.9, "reason": "uses col"}
                    # URN_C omitted: expect an explicit abstention claim
                ]
            }
        ]
    )
    agent = BlastRadiusAgent(FakeMCP(lineage=lineage, schemas=schemas), llm)
    claims = agent.forecast(URN_A, "col")
    by_urn = {c.entity_urn: c for c in claims}
    assert by_urn[URN_B].prediction["will_break"] is True
    assert by_urn[URN_B].claim_type == BLAST_RADIUS
    abstained = by_urn[URN_C]
    assert abstained.confidence == 0.5
    assert abstained.prediction["will_break"] is False
