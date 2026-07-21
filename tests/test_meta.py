"""Meta batch: claims about claims, exercised with fakes and a temp ledger.

Under test: the revert predictor's directional normalization and target
linkage, conflict discovery and pick validation for the arbiter, the
auditor's ledger-vs-catalog diff in both directions, and the mechanical
propagation in metasettle (a meta claim settles exactly when its target
does, with the correct sign).
"""

from __future__ import annotations

import json

from heimdall.agents import (
    DisagreementArbiterAgent,
    RevertPredictorAgent,
    RogueAuditorAgent,
    find_conflicts,
)
from heimdall.claims import (
    ARBITRATION,
    ENRICHMENT,
    REVERT_FORECAST,
    Claim,
    ClaimStore,
)
from heimdall.metasettle import settle_meta_claims

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"


class FakeLLM:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def chat_json(self, system, user, max_tokens=2000, retries=2):
        self.prompts.append(user)
        return self.responses.pop(0)


class FakeMCP:
    def __init__(self, entity_blob=""):
        self.entity_blob = entity_blob

    def get_entities(self, urns):
        return self.entity_blob


def proposal(agent_id, kind="table_doc", column=None, **values) -> Claim:
    prediction = {"kind": kind, **values}
    if column is not None:
        prediction["column"] = column
    return Claim(
        agent_id=agent_id,
        claim_type=ENRICHMENT,
        entity_urn=URN,
        prediction=prediction,
        confidence=0.8,
    )


# -- revert predictor --------------------------------------------------------


def test_revert_predictor_links_targets_and_normalizes(tmp_path):
    store = ClaimStore(tmp_path / "l.db")
    target = store.record(proposal("naive", description="This table stores data."))
    # "will not be reverted at 0.2" really means "will be reverted at 0.8"
    llm = FakeLLM(
        [
            {
                "forecasts": [
                    {
                        "claim_id": target.claim_id,
                        "will_be_reverted": False,
                        "confidence": 0.2,
                    }
                ]
            }
        ]
    )
    agent = RevertPredictorAgent(llm, "revert-predictor")
    claims = agent.forecast(store, [target])
    assert len(claims) == 1
    c = claims[0]
    assert c.claim_type == REVERT_FORECAST
    assert c.prediction["target_claim_id"] == target.claim_id
    assert c.prediction["will_be_reverted"] is True
    assert c.confidence == 0.8
    store.close()


def test_revert_predictor_skips_own_and_omitted(tmp_path):
    store = ClaimStore(tmp_path / "l.db")
    own = proposal("revert-predictor")
    other = store.record(proposal("naive", description="x"))
    llm = FakeLLM([{"forecasts": []}])  # model omits the answer
    agent = RevertPredictorAgent(llm, "revert-predictor")
    assert agent.forecast(store, [own, other]) == []
    store.close()


# -- arbiter -----------------------------------------------------------------


def test_find_conflicts_pairs_only_real_disputes():
    a = proposal("agent-a", description="Raw order feed from the shop system.")
    b = proposal("naive", description="This table stores data.")
    same = proposal("agent-c", description="Raw order feed from the shop system.")
    other_col = proposal("agent-a", kind="pii", column="email", pii_type="email")
    pairs = find_conflicts([a, b, same, other_col])
    assert len(pairs) == 1
    assert {pairs[0][0].agent_id, pairs[0][1].agent_id} == {"agent-a", "naive"}


def test_arbiter_pick_validation_and_claim_shape(tmp_path):
    store = ClaimStore(tmp_path / "l.db")
    a = store.record(proposal("agent-a", description="Specific accurate text."))
    b = store.record(proposal("naive", description="This table stores data."))

    bad = DisagreementArbiterAgent(FakeLLM([{"winner": "C", "confidence": 0.9}]))
    assert bad.arbitrate(store, a, b) is None

    good = DisagreementArbiterAgent(FakeLLM([{"winner": "A", "confidence": 0.85}]))
    claim = good.arbitrate(store, a, b)
    assert claim.claim_type == ARBITRATION
    assert claim.prediction["winner_claim_id"] == a.claim_id
    assert claim.prediction["loser_claim_id"] == b.claim_id
    assert claim.prediction["n_candidates"] == 2
    store.close()


# -- auditor -----------------------------------------------------------------


def _accepted(store, agent_id, text, column=None):
    kind = "column_doc" if column else "table_doc"
    c = store.record(proposal(agent_id, kind=kind, column=column, description=text))
    store.settle(c.claim_id, outcome={"verdict": "accepted"}, correct=True)
    return c


def test_auditor_clean_and_tampered(tmp_path):
    store = ClaimStore(tmp_path / "l.db")
    _accepted(store, "enricher", "Total order amount in USD.", column="total_usd")
    _accepted(store, "table-describer", "Raw order feed from the shop system.")

    intact = json.dumps(
        {
            "description": "Raw order feed from the shop system.",
            "fields": [{"fieldPath": "total_usd", "description": "Total order amount in USD."}],
        }
    )
    clean = RogueAuditorAgent(FakeMCP(intact)).audit(store, URN)
    assert clean.prediction["tampered"] is False

    tampered_blob = intact.replace("Total order amount in USD.", "hacked text")
    flagged = RogueAuditorAgent(FakeMCP(tampered_blob)).audit(store, URN)
    assert flagged.prediction["tampered"] is True
    assert flagged.prediction["missing"] == ["Total order amount in USD."]
    assert flagged.confidence > 0.5
    store.close()


def test_auditor_abstains_without_ledger_expectations(tmp_path):
    store = ClaimStore(tmp_path / "l.db")
    assert RogueAuditorAgent(FakeMCP("{}")).audit(store, URN) is None
    store.close()


# -- metasettle --------------------------------------------------------------


def test_meta_claims_settle_off_their_targets(tmp_path):
    store = ClaimStore(tmp_path / "l.db")
    target = store.record(proposal("naive", description="This table stores data."))

    forecast = store.record(
        Claim(
            agent_id="revert-predictor",
            claim_type=REVERT_FORECAST,
            entity_urn=URN,
            prediction={
                "kind": "revert_forecast",
                "target_claim_id": target.claim_id,
                "will_be_reverted": True,
            },
            confidence=0.8,
        )
    )
    pick = store.record(
        Claim(
            agent_id="disagreement-arbiter",
            claim_type=ARBITRATION,
            entity_urn=URN,
            prediction={
                "kind": "arbitration",
                "winner_claim_id": target.claim_id,
                "loser_claim_id": "other",
                "n_candidates": 2,
            },
            confidence=0.7,
        )
    )

    # nothing settles while the target is open
    assert settle_meta_claims(store) == 0

    store.settle(target.claim_id, outcome={"verdict": "reverted"}, correct=False)
    assert settle_meta_claims(store) == 2

    assert store.get(forecast.claim_id).correct is True  # it said reverted
    assert store.get(pick.claim_id).correct is False  # its pick was rejected
    # idempotent: nothing left to settle
    assert settle_meta_claims(store) == 0
    store.close()
