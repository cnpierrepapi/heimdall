import pytest

from heimdall.claims import (
    BLAST_RADIUS,
    ENRICHMENT,
    FRESHNESS_SLA,
    ROOT_CAUSE,
    Claim,
    ClaimStore,
)
from heimdall.settle import (
    ASSERTION_RESULT,
    INCIDENT_RESOLVED,
    SLA_OUTCOME,
    STEWARD_REVIEW,
    GroundTruthEvent,
    SettlementEngine,
    agent_stats,
    brier,
)

DS = "urn:li:dataset:(urn:li:dataPlatform:dbt,orders,PROD)"
INC = "urn:li:incident:abc"


@pytest.fixture()
def store(tmp_path):
    with ClaimStore(tmp_path / "ledger.db") as s:
        yield s


@pytest.fixture()
def engine(store):
    e = SettlementEngine(store)
    yield e
    e.close()


def record(store, **overrides):
    base = dict(
        agent_id="a1",
        claim_type=BLAST_RADIUS,
        entity_urn=DS,
        prediction={"will_break": True},
        confidence=0.9,
        created_ts=100.0,
    )
    base.update(overrides)
    return store.record(Claim(**base))


def test_blast_radius_settles_true_on_failed_assertion(store, engine):
    claim = record(store)
    settled = engine.process_event(
        GroundTruthEvent(
            event_type=ASSERTION_RESULT,
            entity_urn=DS,
            payload={"passed": False},
            ts=200.0,
        )
    )
    assert [c.claim_id for c in settled] == [claim.claim_id]
    assert settled[0].correct is True
    assert brier(settled[0]) == pytest.approx((0.9 - 1.0) ** 2)


def test_blast_radius_no_break_prediction_wrong_when_assertion_fails(store, engine):
    record(store, prediction={"will_break": False}, confidence=0.7)
    settled = engine.process_event(
        GroundTruthEvent(
            event_type=ASSERTION_RESULT, entity_urn=DS, payload={"passed": False}, ts=200.0
        )
    )
    assert settled[0].correct is False


def test_event_before_claim_does_not_settle(store, engine):
    record(store, created_ts=300.0)
    settled = engine.process_event(
        GroundTruthEvent(
            event_type=ASSERTION_RESULT, entity_urn=DS, payload={"passed": False}, ts=200.0
        )
    )
    assert settled == []
    assert len(store.unsettled()) == 1


def test_event_for_other_entity_does_not_settle(store, engine):
    record(store)
    settled = engine.process_event(
        GroundTruthEvent(
            event_type=ASSERTION_RESULT,
            entity_urn="urn:li:dataset:other",
            payload={"passed": False},
            ts=200.0,
        )
    )
    assert settled == []


def test_freshness_claim_settles_on_sla_outcome(store, engine):
    record(
        store,
        claim_type=FRESHNESS_SLA,
        prediction={"will_miss_sla": True},
        confidence=0.8,
    )
    settled = engine.process_event(
        GroundTruthEvent(
            event_type=SLA_OUTCOME, entity_urn=DS, payload={"missed": True}, ts=150.0
        )
    )
    assert settled[0].correct is True


def test_root_cause_settles_on_incident_resolution(store, engine):
    record(
        store,
        claim_type=ROOT_CAUSE,
        entity_urn=INC,
        prediction={"root_cause_urn": DS, "n_candidates": 4},
        confidence=0.6,
    )
    settled = engine.process_event(
        GroundTruthEvent(
            event_type=INCIDENT_RESOLVED,
            entity_urn=INC,
            payload={"root_cause_urn": DS},
            ts=500.0,
        )
    )
    assert settled[0].correct is True


def test_enrichment_settles_on_steward_review_column_match(store, engine):
    record(
        store,
        claim_type=ENRICHMENT,
        prediction={"column": "email", "description": "Customer email address."},
        confidence=0.85,
    )
    # review of a different column does not settle
    assert (
        engine.process_event(
            GroundTruthEvent(
                event_type=STEWARD_REVIEW,
                entity_urn=DS,
                payload={"column": "phone", "verdict": "accepted"},
                ts=150.0,
            )
        )
        == []
    )
    settled = engine.process_event(
        GroundTruthEvent(
            event_type=STEWARD_REVIEW,
            entity_urn=DS,
            payload={"column": "email", "verdict": "reverted"},
            ts=160.0,
        )
    )
    assert settled[0].correct is False


def test_events_are_persisted(store, engine):
    engine.process_event(
        GroundTruthEvent(
            event_type=SLA_OUTCOME, entity_urn=DS, payload={"missed": False}, ts=1.0
        )
    )
    assert len(engine.events()) == 1


def test_agent_stats_aggregates(store, engine):
    record(store, confidence=0.9)
    record(store, confidence=0.6, prediction={"will_break": False})
    engine.process_event(
        GroundTruthEvent(
            event_type=ASSERTION_RESULT, entity_urn=DS, payload={"passed": False}, ts=200.0
        )
    )
    stats = agent_stats(store)["a1"]
    assert stats["n_settled"] == 2
    assert stats["wins"] == 1
    assert stats["win_rate"] == 0.5
    assert stats["brier_mean"] == pytest.approx(((0.9 - 1) ** 2 + (0.6 - 0) ** 2) / 2)
    assert stats["ece"] is not None
