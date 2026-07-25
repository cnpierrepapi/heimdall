"""H2: the record of what an agent did, which needs no settlement to be true.

Conduct is what the console shows in place of a rank for agents it cannot score.
So the properties that matter are that unscoreable work still produces a full
record, that stopped actions are remembered rather than forgotten, and that a
finding lands on the kind of work that caused it.
"""

from __future__ import annotations

from heimdall.conduct import Conduct, conduct_by_kind, conduct_rows
from heimdall.grounding import SEV_HARMFUL, SEV_WARN, Finding
from heimdall.observability import BLOCKED, HELD, ObservationEvent
from heimdall.simulator.world import build_default_world
from heimdall.workkinds import KIND_COLUMN_DOC, KIND_OWNER, KIND_PII

W = build_default_world()
ORDERS = W.datasets["raw_orders"].urn
CUSTOMERS = W.datasets["raw_customers"].urn


def doc(agent, urn=ORDERS, column="order_total_usd", status="ok", ts=1000.0):
    return ObservationEvent(
        agent_id=agent, tool="update_description", op="write", status=status, ts=ts,
        args={"entity_urn": urn, "column_path": column,
              "description": "Total order amount in usd.", "operation": "replace"},
        entities=[urn],
    )


def own(agent, urn=ORDERS, status="ok", ts=1000.0):
    return ObservationEvent(
        agent_id=agent, tool="add_owners", op="write", status=status, ts=ts,
        args={"entity_urns": [urn], "owner_urns": ["urn:li:corpGroup:marketing"]},
        entities=[urn],
    )


def read(agent, ts=1000.0):
    return ObservationEvent(agent_id=agent, tool="get_dataset", op="read",
                            status="ok", ts=ts, args={"urn": ORDERS}, entities=[ORDERS])


# -- the record exists without settlement -------------------------------------


def test_unscoreable_work_still_produces_a_full_record():
    """An owner agent earns no trust here, but it is not invisible."""
    events = [own("mira", ts=1000.0 + i) for i in range(3)]
    rec = conduct_by_kind(events)[("mira", KIND_OWNER)]
    assert rec.actions == 3 and rec.applied == 3
    assert rec.entities == {ORDERS}


def test_reads_are_not_conduct_for_a_work_kind():
    """Reading a dataset asserts nothing, so it belongs in the feed, not here."""
    assert conduct_by_kind([read("scout")]) == {}


def test_removals_are_not_counted_as_asserted_work():
    remove = ObservationEvent(
        agent_id="nyx", tool="remove_tags", op="write", status="ok",
        args={"entity_urns": [ORDERS], "column_paths": ["email"],
              "tag_urns": ["urn:li:tag:pii-email"]},
    )
    assert conduct_by_kind([remove]) == {}


# -- stopped actions are remembered -------------------------------------------


def test_blocked_and_held_actions_count_as_attempts():
    events = [
        doc("nyx", ts=1.0),
        doc("nyx", status=BLOCKED, ts=2.0),
        doc("nyx", status=HELD, ts=3.0),
    ]
    rec = conduct_by_kind(events)[("nyx", KIND_COLUMN_DOC)]
    assert rec.actions == 3, "an action the gateway stopped is still an attempt"
    assert rec.applied == 1 and rec.blocked == 1 and rec.held == 1


def test_clean_rate_counts_stopped_actions_against_the_agent():
    events = [doc("nyx", ts=1.0), doc("nyx", status=BLOCKED, ts=2.0)]
    assert conduct_by_kind(events)[("nyx", KIND_COLUMN_DOC)].clean_rate == 0.5


def test_clean_rate_is_none_with_no_actions():
    assert Conduct("x", KIND_PII).clean_rate is None


# -- findings land on the work that caused them -------------------------------


def test_a_finding_is_attributed_to_the_kind_that_drew_it():
    d, o = doc("dual", ts=1.0), own("dual", ts=2.0)
    findings = [
        Finding(agent_id="dual", event_id=o.event_id, check_type="owner_domain",
                severity=SEV_HARMFUL, reason="no such team"),
        Finding(agent_id="dual", event_id=d.event_id, check_type="low_quality",
                severity=SEV_WARN, reason="filler"),
    ]
    by_kind = conduct_by_kind([d, o], findings)
    assert by_kind[("dual", KIND_OWNER)].harmful == 1
    assert by_kind[("dual", KIND_OWNER)].warn == 0
    assert by_kind[("dual", KIND_COLUMN_DOC)].warn == 1
    assert by_kind[("dual", KIND_COLUMN_DOC)].harmful == 0


def test_a_finding_with_no_known_action_does_not_invent_a_kind():
    orphan = Finding(agent_id="ghost", event_id=None, check_type="x",
                     severity=SEV_HARMFUL, reason="")
    assert conduct_by_kind([], [orphan]) == {}


# -- the row shape the console consumes ---------------------------------------


def test_agents_rows_include_agents_that_were_only_observed(tmp_path):
    """The leaderboard table is the union of settled and observed, not just settled.

    An owner agent settles nothing here. If agents_rows only walked the claim
    store it would still appear, because unscoreable claims are recorded. This
    pins the stronger property: even with an empty ledger, observed work shows up.
    """
    from heimdall.claims import ClaimStore
    from heimdall.observability import EventStore
    from heimdall.snapshot import agents_rows
    from heimdall.workkinds import UNSCOREABLE

    events = EventStore(str(tmp_path / "e.db"))
    for i in range(3):
        events.record(own("mira", ts=1000.0 + i))
    empty_ledger = ClaimStore(str(tmp_path / "l.db"))

    rows = agents_rows(empty_ledger, catalog="c", event_store=events)

    assert len(rows) == 1
    row = rows[0]
    assert (row["agent_id"], row["work_kind"]) == ("mira", KIND_OWNER)
    assert row["trust"] is None and row["n_settled"] == 0
    assert row["score_state"] == UNSCOREABLE
    assert row["n_actions"] == 3, "no score, but a full conduct record"


def test_conduct_rows_emit_the_columns_hd_agents_carries():
    rows = conduct_rows([doc("atlas"), doc("atlas", urn=CUSTOMERS, column="email")])
    row = rows[("atlas", KIND_COLUMN_DOC)]
    assert row["n_actions"] == 2 and row["n_applied"] == 2
    assert row["n_entities"] == 2, "distinct entities touched, not action count"
    assert set(row) == {"n_actions", "n_applied", "n_blocked", "n_held", "n_errored",
                        "n_harmful", "n_warn", "n_entities", "clean_rate"}


def test_a_write_that_failed_downstream_is_not_counted_as_applied():
    """DataHub can reject a write the gateway already observed and forwarded.

    Calling that "applied" would report a catalog change that never happened.
    """
    events = [doc("nyx", ts=1.0), doc("nyx", status="error", ts=2.0)]
    rec = conduct_by_kind(events)[("nyx", KIND_COLUMN_DOC)]
    assert rec.actions == 2 and rec.applied == 1 and rec.errored == 1
    assert rec.clean_rate == 0.5
