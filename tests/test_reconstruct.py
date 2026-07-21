"""Tests for reconstructing settled claims from a change stream.

The settlement rule is the crux of the reconstruction product, so the unhappy
path (a different actor reverting a write) is covered as a first-class case, not
an afterthought.
"""

import pytest

from heimdall.claims import ClaimStore
from heimdall.provenance import COLUMN_DOC, PII, ProvChange
from heimdall.reconstruct import (
    DEFAULT_RECON_CONFIDENCE,
    SURVIVAL_DAYS,
    load_into_store,
    recon_agent_id,
    reconstruct,
)
from heimdall.settle import brier
from heimdall.skill import skill_report

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_orders,PROD)"
DAY = 86400.0
AI = "urn:li:corpuser:__datahub_system"
HUMAN = "urn:li:corpuser:steward"


def doc(actor, field, value, ts, op="ADD"):
    return ProvChange(
        actor=actor,
        operation=op,
        category="DOCUMENTATION",
        work_kind=COLUMN_DOC,
        dataset_urn=DS,
        field=field,
        target=None,
        value=value,
        ts=ts,
        raw_entity_urn=f"urn:li:schemaField:({DS},{field})",
    )


def test_recon_agent_id_is_actor_and_kind():
    assert recon_agent_id(AI, COLUMN_DOC) == "__datahub_system/column_doc"


def test_survived_write_settles_correct():
    # one write, nothing after it, older than the survival window
    changes = [doc(AI, "order_id", "Primary key.", 0.0)]
    now = SURVIVAL_DAYS * DAY + 1
    (oc,) = reconstruct(changes, now_ts=now)
    assert oc.correct is True
    assert oc.settled_ts == SURVIVAL_DAYS * DAY  # write ts + window


def test_fresh_write_stays_unsettled():
    changes = [doc(AI, "order_id", "Primary key.", 0.0)]
    now = 2 * DAY  # younger than the 7-day window
    (oc,) = reconstruct(changes, now_ts=now)
    assert oc.correct is None
    assert oc.settled_ts is None


def test_revert_by_different_actor_clearing():
    # AI writes, a human blanks it: the AI write is reverted at the clear time
    changes = [
        doc(AI, "order_id", "Primary key.", 0.0),
        doc(HUMAN, "order_id", "", 3 * DAY, op="MODIFY"),
    ]
    outcomes = reconstruct(changes, now_ts=100 * DAY)
    ai_writes = [o for o in outcomes if o.claim.agent_id.startswith("__datahub_system")]
    assert len(ai_writes) == 1
    assert ai_writes[0].correct is False
    assert ai_writes[0].settled_ts == 3 * DAY


def test_revert_by_different_actor_overwriting():
    # a human replaces the text with different text: still a revert of the AI
    changes = [
        doc(AI, "order_id", "wrong description", 0.0),
        doc(HUMAN, "order_id", "correct description", 2 * DAY, op="MODIFY"),
    ]
    outcomes = reconstruct(changes, now_ts=100 * DAY)
    ai = next(o for o in outcomes if o.claim.agent_id.startswith("__datahub_system"))
    assert ai.correct is False


def test_same_actor_revision_is_not_a_revert():
    # the writer edits its own text later: earlier write is left unsettled
    changes = [
        doc(AI, "order_id", "v1", 0.0),
        doc(AI, "order_id", "v2", 3 * DAY, op="MODIFY"),
    ]
    outcomes = reconstruct(changes, now_ts=100 * DAY)
    first = min(outcomes, key=lambda o: o.claim.created_ts)
    assert first.correct is None  # not counted against the writer


def test_reaffirmation_same_value_does_not_revert():
    # a different actor re-adds the same text: not a contradiction; it survives
    changes = [
        doc(AI, "order_id", "same text", 0.0),
        doc(HUMAN, "order_id", "same text", 2 * DAY, op="MODIFY"),
    ]
    outcomes = reconstruct(changes, now_ts=100 * DAY)
    ai = next(o for o in outcomes if o.claim.agent_id.startswith("__datahub_system"))
    assert ai.correct is True


def test_excluded_heimdall_tags_are_skipped():
    tag = ProvChange(
        actor=AI, operation="ADD", category="TAG", work_kind="tag",
        dataset_urn=DS, field=None, target="urn:li:tag:heimdall-unproven",
        value=None, ts=0.0, raw_entity_urn=DS,
    )
    real = ProvChange(
        actor=AI, operation="ADD", category="TAG", work_kind=PII,
        dataset_urn=DS, field="email", target="urn:li:tag:pii-email",
        value=None, ts=0.0, raw_entity_urn=f"urn:li:schemaField:({DS},email)",
    )
    outcomes = reconstruct([tag, real], now_ts=100 * DAY)
    kinds = {o.claim.prediction["kind"] for o in outcomes}
    assert "tag" not in kinds  # the heimdall provenance tag was dropped
    assert PII in kinds


def test_revert_weighs_more_than_survival_in_brier():
    # the confidence asymmetry that makes reverts heavy
    survived = doc(AI, "a", "x", 0.0)
    reverted = doc(AI, "b", "y", 0.0)
    from heimdall.reconstruct import _to_claim

    c_ok = _to_claim(survived, DEFAULT_RECON_CONFIDENCE)
    c_ok.correct = True
    c_bad = _to_claim(reverted, DEFAULT_RECON_CONFIDENCE)
    c_bad.correct = False
    assert brier(c_bad) > 4 * brier(c_ok)  # ~9x at 0.75


def test_load_into_store_and_skill_report(tmp_path):
    # AI writes three that survive; a human reverts one. A rival writer is bad.
    changes = [
        doc(AI, "a", "good", 0.0),
        doc(AI, "b", "good", 0.0),
        doc(AI, "c", "good", 0.0),
        doc(AI, "d", "shaky", 0.0),
        doc(HUMAN, "d", "", 2 * DAY, op="MODIFY"),  # reverts AI's "d"
    ]
    with ClaimStore(tmp_path / "l.db") as store:
        summary = load_into_store(store, changes, now_ts=30 * DAY)
        assert summary["recorded"] == 4
        assert summary["reverted"] == 1
        assert summary["survived"] == 3
        report = skill_report(store, min_settled=1)
        ai_id = recon_agent_id(AI, COLUMN_DOC)
        assert ai_id in report
        rec = report[ai_id]
        assert rec["n_settled"] == 4
        assert rec["wins"] == 3
        assert 0.0 < rec["trust"] <= 100.0
