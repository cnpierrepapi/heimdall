import pytest

from heimdall.claims import (
    BLAST_RADIUS,
    ENRICHMENT,
    AlreadySettledError,
    Claim,
    ClaimStore,
)


@pytest.fixture()
def store(tmp_path):
    with ClaimStore(tmp_path / "ledger.db") as s:
        yield s


def make_claim(**overrides):
    base = dict(
        agent_id="blast-radius-1",
        model_id="qwen/qwen3-32b",
        claim_type=BLAST_RADIUS,
        entity_urn="urn:li:dataset:(urn:li:dataPlatform:dbt,orders,PROD)",
        prediction={"breaks": ["urn:li:dataset:(urn:li:dataPlatform:dbt,rpt,PROD)"]},
        confidence=0.8,
        evidence=["urn:li:query:q1"],
    )
    base.update(overrides)
    return Claim(**base)


def test_roundtrip_preserves_all_fields(store):
    claim = make_claim()
    store.record(claim)
    loaded = store.get(claim.claim_id)
    assert loaded == claim


def test_confidence_must_be_in_unit_interval():
    with pytest.raises(ValueError):
        make_claim(confidence=1.2)
    with pytest.raises(ValueError):
        make_claim(confidence=-0.1)


def test_settle_records_outcome_and_verdict(store):
    claim = store.record(make_claim())
    assert not claim.settled

    settled = store.settle(claim.claim_id, outcome={"broke": True}, correct=True)
    assert settled.settled
    assert settled.outcome == {"broke": True}
    assert settled.correct is True
    assert settled.settled_ts is not None


def test_double_settle_raises(store):
    claim = store.record(make_claim())
    store.settle(claim.claim_id, outcome={}, correct=False)
    with pytest.raises(AlreadySettledError):
        store.settle(claim.claim_id, outcome={}, correct=True)


def test_settle_unknown_claim_raises(store):
    with pytest.raises(KeyError):
        store.settle("nope", outcome={}, correct=True)


def test_unsettled_filter(store):
    a = store.record(make_claim())
    b = store.record(make_claim(claim_type=ENRICHMENT))
    store.settle(a.claim_id, outcome={}, correct=True)

    open_claims = store.unsettled()
    assert [c.claim_id for c in open_claims] == [b.claim_id]
    assert store.unsettled(claim_type=BLAST_RADIUS) == []


def test_claims_filters_by_agent_and_entity(store):
    store.record(make_claim(agent_id="a1"))
    store.record(make_claim(agent_id="a2"))
    store.record(make_claim(agent_id="a2", entity_urn="urn:li:dataset:other"))

    assert len(store.claims(agent_id="a2")) == 2
    assert len(store.claims(entity_urn="urn:li:dataset:other")) == 1
    assert store.agent_ids() == ["a1", "a2"]


def test_summary_counts(store):
    a = store.record(make_claim(agent_id="a1"))
    b = store.record(make_claim(agent_id="a1"))
    store.record(make_claim(agent_id="a2"))
    store.settle(a.claim_id, outcome={}, correct=True)
    store.settle(b.claim_id, outcome={}, correct=False)

    s = store.summary()
    assert s["a1"] == {"total": 2, "settled": 2, "correct": 1}
    assert s["a2"] == {"total": 1, "settled": 0, "correct": 0}
