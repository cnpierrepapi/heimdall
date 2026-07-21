"""Governance batch: agent claim shaping and steward truth, with fakes.

Same philosophy as test_agents.py: the LLM and MCP are canned doubles, and
what is under test is the contract around the judgment call (abstention for
off-list answers, hallucinated columns dropped, confidence clamps, claim
shapes) plus the steward evaluator that derives verdicts from world truth,
including the planted traps: pseudonymous keys and demographics are not PII,
cash settled is not recognized revenue, an event count is not a count of
active customers.
"""

from __future__ import annotations

from heimdall.agents import (
    DomainAssignerAgent,
    NaiveGovernanceAgent,
    OwnerRecommenderAgent,
    PiiTaggerAgent,
    TableDescriberAgent,
    TermMapperAgent,
)
from heimdall.claims import ENRICHMENT, Claim
from heimdall.simulator import steward
from heimdall.simulator.world import build_default_world, dataset_urn

URN_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"

SCHEMA_A = {
    "urn": URN_A,
    "fields": [
        {"fieldPath": "customer_id", "description": "Key."},
        {"fieldPath": "email", "description": ""},
        {"fieldPath": "country", "description": "Country."},
    ],
}


class FakeLLM:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def chat_json(self, system, user, max_tokens=2000, retries=2):
        self.prompts.append(user)
        return self.responses.pop(0)


class FakeMCP:
    def __init__(self, lineage=None, schemas=None):
        self.lineage = lineage or {}
        self.schemas = schemas or {}
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return {"success": True}

    def get_lineage(self, urn, upstream, max_hops=3):
        return self.lineage.get((urn, upstream), {"entities": []})

    def list_schema_fields(self, urn):
        return self.schemas.get(urn, {"urn": urn, "fields": []})


def governance_claim(kind: str, prediction: dict, urn: str) -> Claim:
    return Claim(
        agent_id="t",
        claim_type=ENRICHMENT,
        entity_urn=urn,
        prediction={"kind": kind, **prediction},
        confidence=0.7,
    )


# -- agents ------------------------------------------------------------------


def test_table_describer_claim_shape_and_clamp():
    llm = FakeLLM([{"description": "Raw order feed from the shop.", "confidence": 4.2}])
    agent = TableDescriberAgent(FakeMCP(schemas={URN_A: SCHEMA_A}), llm)
    claims = agent.propose(URN_A)
    assert len(claims) == 1
    c = claims[0]
    assert c.claim_type == ENRICHMENT
    assert c.prediction["kind"] == "table_doc"
    assert c.confidence == 0.95  # clamped from a nonsense value
    assert "description" in c.prediction


def test_table_describer_abstains_on_empty():
    llm = FakeLLM([{"description": "  ", "confidence": 0.8}])
    agent = TableDescriberAgent(FakeMCP(schemas={URN_A: SCHEMA_A}), llm)
    assert agent.propose(URN_A) == []


def test_pii_tagger_drops_hallucinated_columns_and_types():
    llm = FakeLLM(
        [
            {
                "flags": [
                    {"column": "email", "pii_type": "email", "confidence": 0.9},
                    {"column": "ghost_col", "pii_type": "email", "confidence": 0.9},
                    {"column": "country", "pii_type": "location", "confidence": 0.9},
                ]
            }
        ]
    )
    agent = PiiTaggerAgent(FakeMCP(schemas={URN_A: SCHEMA_A}), llm)
    claims = agent.propose(URN_A)
    assert [c.prediction["column"] for c in claims] == ["email"]
    assert claims[0].prediction["kind"] == "pii"


def test_owner_recommender_rejects_off_list_answer():
    llm = FakeLLM([{"owner": "made-up-team", "confidence": 0.9}])
    agent = OwnerRecommenderAgent(FakeMCP(schemas={URN_A: SCHEMA_A}), llm)
    assert agent.propose(URN_A, ["team-a", "team-b"]) == []


def test_owner_recommender_records_n_candidates():
    llm = FakeLLM([{"owner": "team-b", "confidence": 0.8}])
    agent = OwnerRecommenderAgent(FakeMCP(schemas={URN_A: SCHEMA_A}), llm)
    claims = agent.propose(URN_A, ["team-a", "team-b", "team-c"])
    assert claims[0].prediction == {
        "kind": "owner",
        "owner": "team-b",
        "n_candidates": 3,
    }


def test_domain_assigner_needs_two_candidates():
    agent = DomainAssignerAgent(FakeMCP(schemas={URN_A: SCHEMA_A}), FakeLLM([]))
    assert agent.propose(URN_A, ["OnlyDomain"]) == []


def test_term_mapper_dedupes_and_filters():
    llm = FakeLLM(
        [
            {
                "mappings": [
                    {"column": "email", "term": "Customer Email", "confidence": 0.8},
                    {"column": "email", "term": "Customer Email", "confidence": 0.6},
                    {"column": "country", "term": "Not A Term", "confidence": 0.9},
                ]
            }
        ]
    )
    agent = TermMapperAgent(FakeMCP(schemas={URN_A: SCHEMA_A}), llm)
    claims = agent.propose(URN_A, ["Customer Email", "Customer Country"])
    assert len(claims) == 1
    assert claims[0].prediction["term"] == "Customer Email"


def test_naive_rival_is_deterministic_and_hits_the_traps():
    mcp = FakeMCP(schemas={URN_A: SCHEMA_A})
    agent = NaiveGovernanceAgent(mcp, "naive-governance")
    claims = agent.propose_all(
        URN_A,
        teams=["commerce-analytics", "data-platform"],
        domains=["Commerce", "Customers"],
        terms=["Customer Country", "Active Customers"],
    )
    kinds = [c.prediction["kind"] for c in claims]
    assert kinds.count("table_doc") == 1
    assert kinds.count("owner") == 1 and kinds.count("domain") == 1
    # the naive pattern matcher flags the pseudonymous key and the demographic
    pii = {c.prediction["column"]: c.prediction["pii_type"] for c in claims if c.prediction["kind"] == "pii"}
    assert pii["customer_id"] == "national_id"
    assert pii["country"] == "address"
    assert pii["email"] == "email"
    # same input, same claims
    again = agent.propose_all(
        URN_A,
        teams=["commerce-analytics", "data-platform"],
        domains=["Commerce", "Customers"],
        terms=["Customer Country", "Active Customers"],
    )
    assert [c.prediction for c in again] == [c.prediction for c in claims]


# -- world truth and the steward ---------------------------------------------


def test_world_governance_truth_complete():
    w = build_default_world()
    assert w.teams() == ["commerce-analytics", "customer-analytics", "data-platform"]
    assert w.domain_names() == ["Commerce", "Customers", "Engagement"]
    assert set(w.term_names()) == {
        "Gross Order Value",
        "Recognized Revenue",
        "Settled Payment Amount",
        "Customer Country",
        "Active Customers",
    }
    for d in w.datasets.values():
        assert d.owner and d.domain and d.table_keywords


def test_pii_truth_traps():
    w = build_default_world()
    assert w.pii_type("raw_customers", "email") == "email"
    assert w.pii_type("raw_customers", "full_name") == "person_name"
    # the traps: pseudonymous keys and demographics are not PII
    assert w.pii_type("raw_orders", "customer_id") is None
    assert w.pii_type("raw_customers", "country_code") is None


def test_term_truth_traps():
    w = build_default_world()
    assert w.term_for("fct_revenue", "revenue_usd") == "Recognized Revenue"
    # cash settled is not recognized revenue
    assert w.term_for("fct_revenue", "paid_usd") == "Settled Payment Amount"
    # an event count is not "Active Customers"
    assert w.term_for("fct_engagement", "events_30d") is None


def test_steward_settles_each_kind_against_truth():
    w = build_default_world()
    ok = governance_claim(
        "pii", {"column": "email", "pii_type": "email"}, dataset_urn("raw_customers")
    )
    wrong_type = governance_claim(
        "pii",
        {"column": "email", "pii_type": "person_name"},
        dataset_urn("raw_customers"),
    )
    trap = governance_claim(
        "pii",
        {"column": "customer_id", "pii_type": "national_id"},
        dataset_urn("raw_orders"),
    )
    assert steward.evaluate(w, ok) is True
    assert steward.evaluate(w, wrong_type) is False
    assert steward.evaluate(w, trap) is False

    assert (
        steward.evaluate(
            w,
            governance_claim(
                "owner", {"owner": "data-platform"}, dataset_urn("raw_orders")
            ),
        )
        is True
    )
    assert (
        steward.evaluate(
            w,
            governance_claim(
                "domain", {"domain": "Engagement"}, dataset_urn("rpt_daily_kpis")
            ),
        )
        is False  # the blended report files under Commerce
    )
    assert (
        steward.evaluate(
            w,
            governance_claim(
                "table_doc",
                {"description": "Daily KPI rollup for leadership."},
                dataset_urn("rpt_daily_kpis"),
            ),
        )
        is True
    )
    assert (
        steward.evaluate(
            w,
            governance_claim(
                "term",
                {"column": "paid_usd", "term": "Recognized Revenue"},
                dataset_urn("fct_revenue"),
            ),
        )
        is False
    )


def test_steward_still_settles_plain_column_docs():
    w = build_default_world()
    claim = Claim(
        agent_id="t",
        claim_type=ENRICHMENT,
        entity_urn=dataset_urn("raw_orders"),
        prediction={
            "column": "order_total_usd",
            "description": "Total order amount in USD.",
        },
        confidence=0.8,
    )
    assert steward.evaluate(w, claim) is True
