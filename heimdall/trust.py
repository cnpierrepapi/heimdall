"""Per-agent trust scoring from observed writes and grounded findings.

This turns the observation stream into a reliability score. Each write an agent
made through the gateway is a claim that the metadata it wrote is correct. The
catalog-grounded evaluators (grounding.py) decide the outcome: a write that
drew a finding is a revert, a clean write on a surface we can judge is an
accept, a write on a surface with no catalog truth to compare stays unsettled.

Those settled outcomes feed the inherited settle/skill engine unchanged. The
identity unit is (agent x work_kind): one agent may be a skilled column
documenter and a reckless PII tagger, and the score should say so. We encode
that as a composite claim agent id `agent::work_kind`, so skill_report yields a
verdict and trust score per pair, with the pooled machine-metadata acceptance
rate as the luck baseline (beating that, not a coin flip, is what earns
"skilled").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional

from .claims import ENRICHMENT, Claim, ClaimStore
from .grounding import (
    CatalogContext,
    SEV_HARMFUL,
    ground_event,
    parse_action,
)
from .observability import WRITE, ObservationEvent
from .simulator.steward import (
    KIND_COLUMN_DOC,
    KIND_DOMAIN,
    KIND_OWNER,
    KIND_PII,
    KIND_TABLE_DOC,
    KIND_TERM,
)
from .skill import skill_report

SEP = "::"
IMPLICIT_CONFIDENCE = 0.6


@dataclass
class GradedWrite:
    agent_id: str
    work_kind: str
    entity_urn: str
    column: Optional[str]
    correct: Optional[bool]  # True accept, False revert, None not gradeable
    ts: float


def _targets(action) -> list[tuple[str, Optional[str]]]:
    """(work_kind, column) pairs a write asserts, from its tool and args."""
    tool = action.tool
    if tool == "update_description":
        if action.columns:
            return [(KIND_COLUMN_DOC, c) for c in action.columns]
        return [(KIND_TABLE_DOC, None)]
    if tool == "add_tags" and action.pii_types:
        cols = action.columns or [None]
        return [(KIND_PII, c) for c in cols]
    if tool == "add_terms":
        cols = action.columns or [None]
        return [(KIND_TERM, c) for c in cols]
    if tool == "add_owners":
        return [(KIND_OWNER, None)]
    if tool == "set_domains":
        return [(KIND_DOMAIN, None)]
    return []


def _gradeable(kind: str, dataset: str, column: Optional[str], ctx: CatalogContext) -> bool:
    """Is there catalog truth to judge a clean write of this kind against?"""
    if kind == KIND_COLUMN_DOC:
        return bool(column and column in ctx.columns(dataset)
                    and ctx.column_gold_keywords(dataset, column))
    if kind == KIND_PII:
        return bool(column and column in ctx.columns(dataset))
    if kind == KIND_TERM:
        return bool(column and ctx.column_term(dataset, column) is not None)
    if kind == KIND_OWNER:
        return ctx.owner(dataset) is not None
    if kind == KIND_DOMAIN:
        return ctx.domain(dataset) is not None
    return False  # table_doc grading not yet implemented


def graded_targets(event: ObservationEvent, ctx: CatalogContext) -> list[GradedWrite]:
    """Grade each thing a write asserts, consistent with the A2 findings."""
    action = parse_action(event)
    if event.op != WRITE or action.tool.startswith("remove_") or action.operation == "remove":
        return []
    if not action.entity_urn:
        return []
    dataset = ctx.dataset_name(action.entity_urn)
    if dataset is None:
        return []

    findings_by_col: dict[Optional[str], list] = {}
    for f in ground_event(event, ctx):
        findings_by_col.setdefault(f.column, []).append(f)

    out: list[GradedWrite] = []
    for kind, column in _targets(action):
        fs = findings_by_col.get(column, [])
        if any(f.severity == SEV_HARMFUL for f in fs):
            correct: Optional[bool] = False       # a grounded violation
        elif fs:
            correct = False                        # a warn (e.g. low quality) still fails
        elif _gradeable(kind, dataset, column, ctx):
            correct = True                         # clean write on a judgeable surface
        else:
            correct = None                         # nothing to grade against
        out.append(GradedWrite(event.agent_id, kind, action.entity_urn, column, correct, event.ts))
    return out


def composite_id(agent_id: str, work_kind: str) -> str:
    return f"{agent_id}{SEP}{work_kind}"


def split_id(composite: str) -> tuple[str, str]:
    agent, _, kind = composite.partition(SEP)
    return agent, kind


def settle_observations(
    events: list[ObservationEvent], ctx: CatalogContext, store: ClaimStore
) -> dict[str, int]:
    """Record and settle a claim per gradeable write, keyed by agent x kind."""
    counts = {"recorded": 0, "settled": 0, "accepted": 0, "reverted": 0, "unsettled": 0}
    for event in events:
        for gw in graded_targets(event, ctx):
            claim = Claim(
                agent_id=composite_id(gw.agent_id, gw.work_kind),
                model_id="observed",
                claim_type=ENRICHMENT,
                entity_urn=gw.entity_urn,
                prediction={"kind": gw.work_kind, "column": gw.column,
                            "agent": gw.agent_id},
                confidence=IMPLICIT_CONFIDENCE,
                evidence=["observed-write"],
                created_ts=gw.ts,
            )
            store.record(claim)
            counts["recorded"] += 1
            if gw.correct is None:
                counts["unsettled"] += 1
                continue
            store.settle(claim.claim_id, outcome={"grounded": True},
                         correct=gw.correct, settled_ts=gw.ts + 0.001)
            counts["settled"] += 1
            counts["accepted" if gw.correct else "reverted"] += 1
    return counts


def trust_report(store: ClaimStore, **kwargs: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Skill/trust per agent, broken out by work_kind.

    {agent_id: {work_kind: {trust, verdict, n_settled, win_rate, brier_mean, ...}}}
    """
    report = skill_report(store, **kwargs)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for composite, rec in report.items():
        agent, kind = split_id(composite)
        out.setdefault(agent, {})[kind] = rec
    return out


def leaderboard(store: ClaimStore, work_kind: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Agents scored for one work_kind, best trust first."""
    report = skill_report(store, **kwargs)
    rows = []
    for composite, rec in report.items():
        agent, kind = split_id(composite)
        if kind != work_kind:
            continue
        rows.append({"agent_id": agent, **rec})
    rows.sort(key=lambda r: r.get("trust", 0.0), reverse=True)
    return rows


def best_agent_per_kind(store: ClaimStore, **kwargs: Any) -> dict[str, dict[str, Any]]:
    """The global leaderboard by work_kind: the top-trust agent for each kind.

    This is the SELECT answer: point a job of a given kind at the agent that
    has earned the most trust doing exactly that kind of work.
    """
    report = skill_report(store, **kwargs)
    by_kind: dict[str, dict[str, Any]] = {}
    for composite, rec in report.items():
        agent, kind = split_id(composite)
        candidate = {"agent_id": agent, "trust": rec["trust"],
                     "verdict": rec["verdict"], "n_settled": rec["n_settled"]}
        cur = by_kind.get(kind)
        if cur is None or candidate["trust"] > cur["trust"]:
            by_kind[kind] = candidate
    return by_kind


def agent_profile(store: ClaimStore, agent_id: str, **kwargs: Any) -> dict[str, dict[str, Any]]:
    """One agent's trust and verdict across every work_kind it has done."""
    return trust_report(store, **kwargs).get(agent_id, {})


def hd_agents_rows(
    store: ClaimStore,
    registry: Optional[dict[str, dict[str, Any]]] = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Rows for the hd_agents table (one per agent x work_kind).

    registry optionally maps agent_id -> {"visibility": "public"|"private",
    "owner": <org>}; agents default to public with no owner.
    """
    registry = registry or {}
    report = skill_report(store, **kwargs)
    rows = []
    for composite, rec in report.items():
        agent, kind = split_id(composite)
        meta = registry.get(agent, {})
        rows.append({
            "agent_id": agent,
            "work_kind": kind,
            "trust": rec["trust"],
            "verdict": rec["verdict"],
            "n_settled": rec["n_settled"],
            "brier": rec.get("brier_mean"),
            "win_rate": rec.get("win_rate"),
            "visibility": meta.get("visibility", "public"),
            "owner": meta.get("owner"),
        })
    return rows


def score_events(
    events: list[ObservationEvent], ctx: CatalogContext, db_path: str, **kwargs: Any
) -> tuple[dict[str, int], dict[str, dict[str, dict[str, Any]]]]:
    """End to end: settle observations into a fresh ledger and report trust."""
    store = ClaimStore(db_path)
    counts = settle_observations(events, ctx, store)
    report = trust_report(store, **kwargs)
    return counts, report
