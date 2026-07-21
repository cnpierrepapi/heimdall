"""Policy tier decisions from author standing and action findings."""

from __future__ import annotations

from heimdall.grounding import SEV_HARMFUL, SEV_WARN, Finding
from heimdall.policy import (
    TIER_ACCEPT,
    TIER_BLOCK,
    TIER_HOLD,
    TIER_PASS,
    PolicyThresholds,
    decide,
)
from heimdall.skill import HARMFUL, LUCK, SKILLED, UNSETTLED

TH = PolicyThresholds()


def harmful_finding():
    return Finding(agent_id="a", check_type="glossary_conflict",
                   severity=SEV_HARMFUL, reason="contradicts the glossary")


def warn_finding():
    return Finding(agent_id="a", check_type="low_quality_description",
                   severity=SEV_WARN, reason="omits the expected concept")


def d(**kw):
    base = dict(agent_trust=50.0, agent_verdict=UNSETTLED, n_settled=0,
                findings=[], min_trust=0.0, thresholds=TH)
    base.update(kw)
    return decide(**base)


def test_harmful_action_blocks_even_for_clean_author():
    assert d(agent_trust=90.0, agent_verdict=SKILLED, n_settled=50,
             findings=[harmful_finding()]).tier == TIER_BLOCK


def test_harmful_verdict_blocks():
    dec = d(agent_verdict=HARMFUL, n_settled=20)
    assert dec.tier == TIER_BLOCK and "worse than chance" in dec.reason


def test_hard_min_trust_floor_blocks():
    dec = d(agent_trust=50.0, min_trust=55.0)
    assert dec.tier == TIER_BLOCK and "heimdall policy" in dec.reason


def test_warn_action_holds():
    assert d(findings=[warn_finding()]).tier == TIER_HOLD


def test_proven_mediocre_author_holds_clean_write():
    assert d(agent_trust=48.0, agent_verdict=LUCK, n_settled=10).tier == TIER_HOLD


def test_trusted_author_clean_write_auto_accepts():
    assert d(agent_trust=80.0, agent_verdict=SKILLED, n_settled=30).tier == TIER_ACCEPT


def test_unproven_clean_write_passes():
    assert d(agent_trust=50.0, n_settled=0).tier == TIER_PASS


def test_action_findings_take_priority_over_standing():
    # a trusted author making a harmful write is still blocked
    dec = d(agent_trust=95.0, agent_verdict=SKILLED, n_settled=100,
            findings=[harmful_finding()])
    assert dec.tier == TIER_BLOCK
    assert dec.reason == "contradicts the glossary"
