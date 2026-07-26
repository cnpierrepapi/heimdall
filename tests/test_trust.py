"""Per-agent, per-work-kind trust scoring from observed writes."""

from __future__ import annotations

from heimdall.claims import ClaimStore
from heimdall.grounding import WorldCatalogContext
from heimdall.observability import ObservationEvent
from heimdall.simulator.steward import KIND_COLUMN_DOC, KIND_OWNER, KIND_PII
from heimdall.simulator.world import build_default_world
from heimdall.skill import HARMFUL, SKILLED
from heimdall.trust import (
    SurfaceLedger,
    agent_profile,
    best_agent_per_kind,
    composite_id,
    graded_targets,
    hd_agents_rows,
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


def test_owner_writes_are_observed_but_never_settled():
    """Ownership settles by steward review, which this deployment does not have.

    The catalog does carry an owner, so it would be easy to grade a guess against
    it. That is exactly the trap: the label was assigned, not derived, so nothing
    the agent can read points to it and a correct guess is luck. The write is
    still graded as a target (it is observed) but carries no outcome.
    """
    wrong = ev("add_owners", {"entity_urns": [ORDERS],
                              "owner_urns": ["urn:li:corpGroup:marketing"]})
    g = one(wrong)
    assert g.work_kind == KIND_OWNER
    assert g.correct is None, "an unscoreable kind must not bank a revert"

    right = ev("add_owners", {"entity_urns": [ORDERS],
                              "owner_urns": ["urn:li:corpGroup:data-platform"]})
    assert one(right).correct is None, "nor an accept"


def test_owner_violations_still_surface_as_findings():
    """Withholding the score must not withhold the oversight."""
    from heimdall.grounding import ground_event
    wrong = ev("add_owners", {"entity_urns": [ORDERS],
                              "owner_urns": ["urn:li:corpGroup:marketing"]})
    assert ground_event(wrong, CTX), "a wrong owner is still caught and cited"


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


def _agent_events(agent, n_correct):
    """n_correct correct descriptions then filler for the rest of GOLD_COLS."""
    out = []
    for i, (urn, col, desc) in enumerate(GOLD_COLS):
        text = desc if i < n_correct else "a column here"
        out.append(ev("update_description",
                      {"entity_urn": urn, "column_path": col,
                       "description": text, "operation": "replace"}, agent=agent))
    return out


def _ranked_store(tmp_path):
    store = ClaimStore(str(tmp_path / "l.db"))
    events = (_agent_events("expert-doc", 5)
              + _agent_events("mid-doc", 3)
              + _agent_events("rogue-doc", 0))
    settle_observations(events, CTX, store)
    return store


def test_best_agent_per_kind_picks_top_trust(tmp_path):
    store = _ranked_store(tmp_path)
    best = best_agent_per_kind(store)
    # selection is by earned trust; expert has the best record for this kind
    assert best[KIND_COLUMN_DOC]["agent_id"] == "expert-doc"
    assert best[KIND_COLUMN_DOC]["verdict"] != HARMFUL


def test_full_ranking_order(tmp_path):
    store = _ranked_store(tmp_path)
    board = leaderboard(store, KIND_COLUMN_DOC)
    assert [r["agent_id"] for r in board] == ["expert-doc", "mid-doc", "rogue-doc"]


def test_hd_agents_rows_shape(tmp_path):
    store = _ranked_store(tmp_path)
    rows = hd_agents_rows(store)
    assert {r["agent_id"] for r in rows} == {"expert-doc", "mid-doc", "rogue-doc"}
    assert all(r["work_kind"] == KIND_COLUMN_DOC for r in rows)
    assert all({"trust", "verdict", "n_settled"} <= set(r) for r in rows)


def test_agent_profile_spans_kinds(tmp_path):
    store = ClaimStore(str(tmp_path / "l.db"))
    events = [good_write(*c) for c in GOLD_COLS]  # column_doc
    events.append(ev("add_owners",
                     {"entity_urns": [ORDERS], "owner_urns": ["urn:li:corpGroup:data-platform"]},
                     agent="good-agent"))  # owner, correct
    settle_observations(events, CTX, store)
    profile = agent_profile(store, "good-agent")
    assert KIND_COLUMN_DOC in profile


# -- new artifacts only -------------------------------------------------------
#
# Scoring counts work on artifacts that were new when the agent reached them.
# These lock the two halves of that: what the catalog already carried, and what
# had already been written by the time the day began.


def _tiny_ctx(described: bool):
    """A one-column catalog whose single gold column is documented or not."""
    from heimdall.catalog import CatalogSpec, ColumnSpec, DatasetSpec, spec_to_world
    spec = CatalogSpec(
        catalog="hcatalog_test",
        theme="test",
        datasets=[DatasetSpec(
            name="raw_sales",
            owner="data-platform",
            columns=[
                ColumnSpec(name="net_total_usd",
                           description="Net sale total in usd." if described else None,
                           gold_keywords=["total", "usd"]),
                ColumnSpec(name="buyer_email", pii="email"),
            ],
        )],
    )
    world = spec_to_world(spec)
    return WorldCatalogContext(world), world.datasets["raw_sales"].urn


def _doc(urn, col, desc, agent="a1"):
    return ev("update_description", {"entity_urn": urn, "column_path": col,
                                     "description": desc, "operation": "replace"}, agent=agent)


def _tag(urn, col, tag="urn:li:tag:pii-email", agent="a1", status="ok"):
    e = ev("add_tags", {"entity_urns": [urn], "column_paths": [col], "tag_urns": [tag]},
           agent=agent)
    return e.model_copy(update={"status": status})


def test_a_column_the_catalog_already_documents_is_not_scored():
    """A correct description of a documented column is not new work.

    The answer was already published in the artifact the agent read, so an accept
    here would credit text the agent did not have to derive from anything.
    """
    ctx, urn = _tiny_ctx(described=True)
    gs = graded_targets(_doc(urn, "net_total_usd", "Net sale total in usd."), ctx)
    assert len(gs) == 1
    assert gs[0].correct is None and gs[0].rewrite is True


def test_the_same_column_undocumented_is_scored():
    """The control: identical write, blank column, and it grades."""
    ctx, urn = _tiny_ctx(described=False)
    gs = graded_targets(_doc(urn, "net_total_usd", "Net sale total in usd."), ctx)
    assert gs[0].correct is True and gs[0].rewrite is False


def test_two_agents_on_the_same_new_column_are_both_scored(tmp_path):
    """Cast order must not decide who gets scored.

    Occupancy is judged as of the start of the day, so both agents answer the same
    question from the same state. If the first write consumed the surface the
    leaderboard would be a race, and the second agent's real work would vanish.
    """
    ctx, urn = _tiny_ctx(described=False)
    store = ClaimStore(str(tmp_path / "l.db"))
    counts = settle_observations(
        [_doc(urn, "net_total_usd", "Net sale total in usd.", agent="first"),
         _doc(urn, "net_total_usd", "a column", agent="second")],
        ctx, store,
    )
    assert counts["settled"] == 2
    assert counts["accepted"] == 1 and counts["reverted"] == 1
    assert counts["rewrite"] == 0


def test_one_agent_writing_twice_in_a_day_is_scored_once(tmp_path):
    ctx, urn = _tiny_ctx(described=False)
    store = ClaimStore(str(tmp_path / "l.db"))
    counts = settle_observations(
        [_doc(urn, "net_total_usd", "Net sale total in usd.", agent="a1"),
         _doc(urn, "net_total_usd", "Net sale total in usd, again.", agent="a1")],
        ctx, store,
    )
    assert counts["recorded"] == 2, "both writes are still observed"
    assert counts["settled"] == 1 and counts["rewrite"] == 1


def test_yesterdays_landed_work_is_not_scored_again(tmp_path):
    """The persistent-world defect: a day-two rewrite must not settle.

    Without this the same judgment call is banked once per simulated day, and the
    n/(n+20) shrinkage reads the repetition as accumulated evidence.
    """
    ctx, urn = _tiny_ctx(described=False)
    yesterday = _tag(urn, "buyer_email", agent="tagger")
    ledger = SurfaceLedger.as_of([yesterday], before_ts=yesterday.ts + 1)

    store = ClaimStore(str(tmp_path / "l.db"))
    counts = settle_observations([_tag(urn, "buyer_email", agent="tagger")],
                                ctx, store, ledger=ledger)
    assert counts["settled"] == 0 and counts["rewrite"] == 1


def test_a_repeat_wrong_flag_cannot_bank_evidence_twice(tmp_path):
    """Two days of the same false PII flag must leave n_settled at one."""
    ctx, urn = _tiny_ctx(described=False)
    wrong = _tag(urn, "net_total_usd", agent="orion-pii")  # not PII in the catalog
    store = ClaimStore(str(tmp_path / "l.db"))

    settle_observations([wrong], ctx, store)  # day one
    ledger = SurfaceLedger.as_of([wrong], before_ts=wrong.ts + 1)
    settle_observations([_tag(urn, "net_total_usd", agent="orion-pii")],
                        ctx, store, ledger=ledger)  # day two, same call

    rec = trust_report(store)["orion-pii"][KIND_PII]
    assert rec["n_settled"] == 1


def test_a_blocked_write_leaves_the_artifact_new(tmp_path):
    """A write the gateway stopped never reached DataHub, so the column is blank.

    The blocked attempt is still scored against the agent that made it; what it
    must not do is consume the surface an honest agent works next.
    """
    ctx, urn = _tiny_ctx(described=False)
    blocked = _tag(urn, "net_total_usd", agent="orion-pii", status="blocked")
    assert graded_targets(blocked, ctx)[0].correct is False, "the attempt still counts"

    ledger = SurfaceLedger.as_of([blocked], before_ts=blocked.ts + 1)
    store = ClaimStore(str(tmp_path / "l.db"))
    counts = settle_observations([_doc(urn, "net_total_usd", "Net sale total in usd.")],
                                ctx, store, ledger=ledger)
    assert counts["accepted"] == 1 and counts["rewrite"] == 0


def test_removing_a_description_hands_the_surface_back(tmp_path):
    """W2's doc rot is what refills the work queue, so removal must free a surface."""
    ctx, urn = _tiny_ctx(described=False)
    wrote = _doc(urn, "net_total_usd", "Net sale total in usd.")
    removed = ev("update_description",
                 {"entity_urn": urn, "column_path": "net_total_usd", "operation": "remove"},
                 agent="rot")
    removed = removed.model_copy(update={"ts": wrote.ts + 1})

    filled = SurfaceLedger.as_of([wrote], before_ts=wrote.ts + 5)
    assert not filled.is_new((urn, KIND_COLUMN_DOC, "net_total_usd"), "someone")
    after = SurfaceLedger.as_of([wrote, removed], before_ts=wrote.ts + 5)
    assert after.is_new((urn, KIND_COLUMN_DOC, "net_total_usd"), "someone")


def test_rewrite_is_distinct_from_unscoreable(tmp_path):
    """The two nulls must stay apart: no steward here, versus no new artifact."""
    ctx, urn = _tiny_ctx(described=False)
    store = ClaimStore(str(tmp_path / "l.db"))
    counts = settle_observations(
        [ev("add_owners", {"entity_urns": [urn],
                           "owner_urns": ["urn:li:corpGroup:data-platform"]}, agent="mira")],
        ctx, store,
    )
    assert counts["unsettled"] == 1 and counts["rewrite"] == 0


def test_a_rewrite_is_still_recorded_for_audit(tmp_path):
    """Withholding the score must not withhold the record of the work."""
    ctx, urn = _tiny_ctx(described=True)
    store = ClaimStore(str(tmp_path / "l.db"))
    settle_observations([_doc(urn, "net_total_usd", "Net sale total in usd.")], ctx, store)
    claims = store.claims(agent_id=composite_id("a1", KIND_COLUMN_DOC))
    assert len(claims) == 1
    assert claims[0].prediction["rewrite"] is True
    assert claims[0].correct is None


def test_a_blocked_attempt_is_scored_once_not_once_a_day(tmp_path):
    """The live defect this caught: a refused write never lands, so it never
    fills the surface, so the same refused judgment came back for grading every
    simulated day. Five days of one rogue tagger reached n=46 on a handful of
    columns before this held.
    """
    ctx, urn = _tiny_ctx(described=False)
    store = ClaimStore(str(tmp_path / "l.db"))
    attempts = []
    for day in range(4):  # four days, same agent, same wrong call, all blocked
        blocked = _tag(urn, "net_total_usd", agent="orion-pii", status="blocked")
        blocked = blocked.model_copy(update={"ts": 1000.0 + day})
        ledger = SurfaceLedger.as_of(attempts, before_ts=blocked.ts)
        settle_observations([blocked], ctx, store, ledger=ledger)
        attempts.append(blocked)

    rec = trust_report(store)["orion-pii"][KIND_PII]
    assert rec["n_settled"] == 1, "one judgment call banked once, not once a day"


def test_a_blocked_attempt_still_leaves_the_column_open_for_others(tmp_path):
    """Burning the attempting agent's turn must not burn everyone else's."""
    ctx, urn = _tiny_ctx(described=False)
    blocked = _tag(urn, "net_total_usd", agent="orion-pii", status="blocked")
    ledger = SurfaceLedger.as_of([blocked], before_ts=blocked.ts + 1)

    store = ClaimStore(str(tmp_path / "l.db"))
    counts = settle_observations(
        [_doc(urn, "net_total_usd", "Net sale total in usd.", agent="atlas-doc")],
        ctx, store, ledger=ledger)
    assert counts["accepted"] == 1 and counts["rewrite"] == 0
