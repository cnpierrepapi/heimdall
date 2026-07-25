"""H1: what may be scored here, and the two different reasons it may not be.

The distinction under test is between an agent that needs more evidence and one
whose work this deployment can never settle. Collapsing them into a single "not
enough data" would promise a verdict that is never coming, and grading the second
kind anyway would publish luck with a number attached.
"""

from __future__ import annotations

import inspect

from heimdall import skill
from heimdall.workkinds import (
    DEFAULT_MIN_SETTLED,
    DEFAULT_SOURCES,
    INSUFFICIENT,
    KIND_COLUMN_DOC,
    KIND_DOMAIN,
    KIND_OWNER,
    KIND_PII,
    KIND_TERM,
    S_ARTIFACT,
    S_FORWARD,
    S_STEWARD,
    SCORED,
    UNSCOREABLE,
    WORK_KINDS,
    available_sources,
    score_state,
    scoreable,
    settlement_of,
)

STEWARDLESS = DEFAULT_SOURCES


# -- the registry is the single source of truth -------------------------------


def test_every_work_kind_declares_what_settles_it():
    for kind, wk in WORK_KINDS.items():
        assert wk.id == kind
        assert wk.settlement, f"{kind} must say what settles it"
        assert wk.summary


def test_roster_and_steward_share_the_registry_constants():
    """The kinds were declared in two places once; a drift here scores the wrong thing."""
    from heimdall import roster
    from heimdall.simulator import steward

    for mod in (roster, steward):
        assert mod.KIND_COLUMN_DOC == KIND_COLUMN_DOC
        assert mod.KIND_PII == KIND_PII
        assert mod.KIND_OWNER == KIND_OWNER


def test_min_settled_matches_the_skill_engine():
    """If skill_report's threshold moves, our reported reason must move with it."""
    default = inspect.signature(skill.skill_report).parameters["min_settled"].default
    assert DEFAULT_MIN_SETTLED == default


# -- scoreability -------------------------------------------------------------


def test_artifact_settled_kinds_are_scoreable_here():
    for kind in (KIND_COLUMN_DOC, KIND_PII):
        assert settlement_of(kind) == S_ARTIFACT
        ok, why = scoreable(kind, STEWARDLESS)
        assert ok and why == ""


def test_steward_settled_kinds_are_not_scoreable_here():
    for kind in (KIND_OWNER, KIND_DOMAIN, KIND_TERM):
        assert settlement_of(kind) == S_STEWARD
        ok, why = scoreable(kind, STEWARDLESS)
        assert not ok
        assert "steward review" in why and "not available" in why


def test_an_unknown_kind_is_never_scoreable():
    ok, why = scoreable("vibes", STEWARDLESS)
    assert not ok and "unknown work kind" in why


def test_a_deployment_with_stewards_can_score_ownership():
    """The cut is a property of this deployment, not of the work kind."""
    with_steward = STEWARDLESS | {S_STEWARD}
    assert scoreable(KIND_OWNER, with_steward)[0]


def test_sources_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("HEIMDALL_SETTLEMENT_SOURCES", f"{S_ARTIFACT},{S_FORWARD}")
    assert available_sources() == frozenset({S_ARTIFACT, S_FORWARD})
    monkeypatch.delenv("HEIMDALL_SETTLEMENT_SOURCES")
    assert available_sources() == DEFAULT_SOURCES


# -- the two nulls ------------------------------------------------------------


def test_low_evidence_reads_as_insufficient_and_says_how_much_is_missing():
    state, reason = score_state(KIND_COLUMN_DOC, n_settled=2, sources=STEWARDLESS)
    assert state == INSUFFICIENT
    assert "2 settled of 5 needed" in reason


def test_enough_evidence_reads_as_scored_with_no_caveat():
    state, reason = score_state(KIND_COLUMN_DOC, n_settled=5, sources=STEWARDLESS)
    assert state == SCORED and reason == ""


def test_unscoreable_beats_evidence_count_in_both_directions():
    """Waiting does not fix an unscoreable kind, so n must not change its state."""
    for n in (0, 5, 500):
        state, reason = score_state(KIND_OWNER, n_settled=n, sources=STEWARDLESS)
        assert state == UNSCOREABLE, f"n={n} must not promote an unscoreable kind"
        assert "steward review" in reason


def test_the_two_nulls_are_distinguishable():
    insufficient, _ = score_state(KIND_COLUMN_DOC, 1, sources=STEWARDLESS)
    unscoreable, _ = score_state(KIND_OWNER, 1, sources=STEWARDLESS)
    assert insufficient != unscoreable


# -- the gate in practice -----------------------------------------------------


def _owner_write(agent: str, urn: str, owner: str, ts: float):
    from heimdall.observability import ObservationEvent
    return ObservationEvent(
        agent_id=agent, tool="add_owners", op="write", status="ok", ts=ts,
        args={"entity_urns": [urn], "owner_urns": [f"urn:li:corpGroup:{owner}"]},
    )


def test_an_agent_cannot_farm_trust_from_ownership_guesses(tmp_path):
    """The roster is not what protects the score: the settlement gate is.

    A third party agent reaches the gateway by config alone, so one doing
    ownership work must be unable to accumulate a record, however lucky it gets.
    """
    from heimdall.claims import ClaimStore
    from heimdall.grounding import WorldCatalogContext
    from heimdall.simulator.world import build_default_world
    from heimdall.trust import settle_observations, trust_report

    world = build_default_world()
    ctx = WorldCatalogContext(world)
    urn = world.datasets["raw_orders"].urn

    # twenty correct ownership calls, far past any evidence threshold
    events = [_owner_write("outsider", urn, "data-platform", 1000.0 + i) for i in range(20)]
    store = ClaimStore(str(tmp_path / "l.db"))
    counts = settle_observations(events, ctx, store)

    assert counts["settled"] == 0, "no ownership claim may settle here"
    assert counts["unsettled"] == 20

    # the attempts are still on the record, which is what conduct is made of
    rec = trust_report(store)["outsider"][KIND_OWNER]
    assert rec["n_settled"] == 0
    assert rec["trust"] == 50.0, "twenty lucky guesses must not move the prior"
    assert rec["score_state"] == UNSCOREABLE
    assert "steward review" in rec["score_reason"]


def test_hd_agents_rows_carry_the_score_state(tmp_path):
    from heimdall.claims import ClaimStore
    from heimdall.grounding import WorldCatalogContext
    from heimdall.observability import ObservationEvent
    from heimdall.simulator.world import build_default_world
    from heimdall.trust import hd_agents_rows, settle_observations

    world = build_default_world()
    ctx = WorldCatalogContext(world)
    urn = world.datasets["raw_orders"].urn
    events = [
        ObservationEvent(
            agent_id="atlas", tool="update_description", op="write", status="ok",
            ts=1000.0 + i,
            args={"entity_urn": urn, "column_path": "order_total_usd",
                  "description": "Total order amount in usd.", "operation": "replace"},
        )
        for i in range(2)
    ]
    store = ClaimStore(str(tmp_path / "l.db"))
    settle_observations(events, ctx, store)

    rows = hd_agents_rows(store)
    assert rows and all("score_state" in r for r in rows)
    row = rows[0]
    assert row["work_kind"] == KIND_COLUMN_DOC
    assert row["score_state"] == INSUFFICIENT
    assert "needed" in row["score_reason"]
