"""Per-agent, per-work-kind trust scoring from observed writes."""

from __future__ import annotations

from heimdall.claims import ClaimStore
from heimdall.grounding import WorldCatalogContext
from heimdall.observability import ObservationEvent
from heimdall.simulator.steward import KIND_COLUMN_DOC, KIND_OWNER, KIND_PII
from heimdall.simulator.world import build_default_world
from heimdall.skill import HARMFUL, SKILLED
from heimdall.trust import (
    graded_targets,
    leaderboard,
    settle_observations,
    trust_report,
)

CTX = WorldCatalogContext(build_default_world())
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_orders,PROD)"
PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_payments,PROD)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_customers,PROD)"


def ev(tool, args, agent="agent", op="write"):
    return ObservationEvent(agent_id=agent, tool=tool, op=op, status="ok", args=args)


def one(event):
    gs = graded_targets(event, CTX)
    assert len(gs) == 1
    return gs[0]


# -- grading ------------------------------------------------------------------


def test_correct_description_grades_accept():
    g = one(ev("update_description", {"entity_urn": ORDERS, "column_path": "order_total_usd",
                                      "description": "Total order amount in usd.",
                                      "operation": "replace"}))
    assert g.work_kind == KIND_COLUMN_DOC and g.column == "order_total_usd"
    assert g.correct is True


def test_glossary_conflict_grades_revert():
    g = one(ev("update_description", {"entity_urn": PAYMENTS, "column_path": "amount_usd",
                                      "description": "The gross order value in usd.",
                                      "operation": "replace"}))
    assert g.correct is False


def test_filler_description_grades_revert():
    g = one(ev("update_description", {"entity_urn": ORDERS, "column_path": "order_total_usd",
                                      "description": "a column", "operation": "replace"}))
    assert g.correct is False


def test_ungradeable_column_doc_is_none():
    # order_id has a description but no gold keywords and no term: nothing to judge
    g = one(ev("update_description", {"entity_urn": ORDERS, "column_path": "order_id",
                                      "description": "The identifier.", "operation": "replace"}))
    assert g.work_kind == KIND_COLUMN_DOC and g.correct is None


def test_correct_pii_grades_accept():
    g = one(ev("add_tags", {"entity_urns": [CUSTOMERS], "column_paths": ["email"],
                            "tag_urns": ["urn:li:tag:pii-email"]}))
    assert g.work_kind == KIND_PII and g.correct is True


def test_false_pii_grades_revert():
    g = one(ev("add_tags", {"entity_urns": [ORDERS], "column_paths": ["customer_id"],
                            "tag_urns": ["urn:li:tag:pii-email"]}))
    assert g.work_kind == KIND_PII and g.correct is False


def test_wrong_owner_grades_revert():
    g = one(ev("add_owners", {"entity_urns": [ORDERS], "owner_urns": ["urn:li:corpGroup:marketing"]}))
    assert g.work_kind == KIND_OWNER and g.correct is False


def test_removal_and_reads_not_graded():
    assert graded_targets(ev("remove_tags", {"entity_urns": [ORDERS], "column_paths": ["email"],
                                             "tag_urns": ["urn:li:tag:pii-email"]}), CTX) == []
    assert graded_targets(ev("get_entities", {"urns": [ORDERS]}, op="read"), CTX) == []


# -- settlement + scoring -----------------------------------------------------

# columns that carry gold keywords, so a description is gradeable
GOLD_COLS = [
    (ORDERS, "order_total_usd", "Total order amount in usd."),
    (ORDERS, "discount_code", "Promo discount coupon code."),
    (PAYMENTS, "amount_usd", "Amount paid in usd, settled."),
    (CUSTOMERS, "email", "Customer email address."),
    (CUSTOMERS, "country_code", "Customer country iso code."),
]


def good_write(urn, col, desc):
    return ev("update_description", {"entity_urn": urn, "column_path": col,
                                     "description": desc, "operation": "replace"},
              agent="good-agent")


def bad_write(urn, col):
    return ev("update_description", {"entity_urn": urn, "column_path": col,
                                     "description": "a column here", "operation": "replace"},
              agent="rogue-agent")


def test_settle_counts(tmp_path):
    store = ClaimStore(str(tmp_path / "l.db"))
    events = [good_write(*c) for c in GOLD_COLS] + [bad_write(u, c) for u, c, _ in GOLD_COLS]
    counts = settle_observations(events, CTX, store)
    assert counts["recorded"] == 10
    assert counts["accepted"] == 5 and counts["reverted"] == 5


def test_good_agent_skilled_rogue_harmful(tmp_path):
    store = ClaimStore(str(tmp_path / "l.db"))
    # 5 correct + 5 filler descriptions on the same gold columns
    events = [good_write(*c) for c in GOLD_COLS] + [bad_write(u, c) for u, c, _ in GOLD_COLS]
    settle_observations(events, CTX, store)
    report = trust_report(store)

    good = report["good-agent"][KIND_COLUMN_DOC]
    rogue = report["rogue-agent"][KIND_COLUMN_DOC]
    assert good["verdict"] == SKILLED
    assert rogue["verdict"] == HARMFUL
    assert good["trust"] > rogue["trust"]


def test_leaderboard_orders_by_trust(tmp_path):
    store = ClaimStore(str(tmp_path / "l.db"))
    events = [good_write(*c) for c in GOLD_COLS] + [bad_write(u, c) for u, c, _ in GOLD_COLS]
    settle_observations(events, CTX, store)
    board = leaderboard(store, KIND_COLUMN_DOC)
    assert [r["agent_id"] for r in board] == ["good-agent", "rogue-agent"]
