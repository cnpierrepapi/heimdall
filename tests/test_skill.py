import random

import pytest

from heimdall.claims import BLAST_RADIUS, ROOT_CAUSE, Claim, ClaimStore
from heimdall.skill import (
    HARMFUL,
    LUCK,
    SKILLED,
    UNSETTLED,
    null_probability,
    skill_report,
    trust_score,
)


@pytest.fixture()
def store(tmp_path):
    with ClaimStore(tmp_path / "ledger.db") as s:
        yield s


def seed_agent(store, agent_id, n, hit_rate, rng, confidence=0.75):
    """Record n settled directional claims with the given empirical hit rate."""
    for i in range(n):
        claim = store.record(
            Claim(
                agent_id=agent_id,
                claim_type=BLAST_RADIUS,
                entity_urn=f"urn:li:dataset:d{i}",
                prediction={"will_break": True},
                confidence=confidence,
                created_ts=1.0,
            )
        )
        store.settle(claim.claim_id, outcome={}, correct=rng.random() < hit_rate)


def test_skilled_agent_detected_and_coin_flipper_not(store):
    rng = random.Random(1)
    seed_agent(store, "sharp", n=80, hit_rate=0.85, rng=rng)
    seed_agent(store, "coin", n=80, hit_rate=0.5, rng=rng)

    report = skill_report(store, n_sims=4000, seed=2)
    assert report["sharp"]["verdict"] == SKILLED
    assert report["coin"]["verdict"] == LUCK
    assert report["sharp"]["trust"] > report["coin"]["trust"]


def test_worse_than_chance_agent_flagged(store):
    rng = random.Random(3)
    seed_agent(store, "inverse", n=80, hit_rate=0.15, rng=rng)
    report = skill_report(store, n_sims=4000, seed=4)
    assert report["inverse"]["verdict"] == HARMFUL


def test_small_sample_lucky_streak_is_not_called_skilled(store):
    rng = random.Random(5)
    seed_agent(store, "lucky3", n=3, hit_rate=1.0, rng=rng)
    report = skill_report(store, n_sims=2000, seed=6)
    assert report["lucky3"]["verdict"] == UNSETTLED
    # 3-for-3 must not outrank a proven long record
    seed_agent(store, "proven", n=100, hit_rate=0.8, rng=rng)
    report = skill_report(store, n_sims=2000, seed=6)
    assert report["proven"]["trust"] > report["lucky3"]["trust"]


def test_null_probability_by_claim_type(store):
    directional = Claim(
        agent_id="a",
        claim_type=BLAST_RADIUS,
        entity_urn="u",
        prediction={"will_break": True},
        confidence=0.9,
    )
    root_cause = Claim(
        agent_id="a",
        claim_type=ROOT_CAUSE,
        entity_urn="u",
        prediction={"root_cause_urn": "x", "n_candidates": 5},
        confidence=0.6,
    )
    assert null_probability(directional, 0.5) == 0.5
    assert null_probability(root_cause, 0.5) == pytest.approx(0.2)


def test_trust_score_neutral_with_no_record():
    assert trust_score([]) == 50.0
