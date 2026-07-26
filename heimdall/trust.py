"""Per-agent trust scoring from observed writes and grounded findings.

This turns the observation stream into a reliability score. Each write an agent
made through the gateway is a claim that the metadata it wrote is correct. The
catalog-grounded evaluators (grounding.py) decide the outcome: a write that
drew a finding is a revert, a clean write on a surface we can judge is an
accept, a write on a surface with no catalog truth to compare stays unsettled.

Only work on a new artifact is scored. A write onto a surface that already
carried metadata of that kind is observed, grounded and governed like any other,
but it earns neither an accept nor a revert. Once worlds persist this stops being
a nicety: an agent that re-flags the same column every day would bank one
judgment call as fresh evidence over and over, and the n/(n+20) shrinkage would
read that duplication as accumulated skill. Nothing about the agent changed; only
the number of days did. See SurfaceLedger for how occupancy is decided.

Those settled outcomes feed the inherited settle/skill engine unchanged. The
identity unit is (agent x work_kind): one agent may be a skilled column
documenter and a reckless PII tagger, and the score should say so. We encode
that as a composite claim agent id `agent::work_kind`, so skill_report yields a
verdict and trust score per pair, with the pooled machine-metadata acceptance
rate as the luck baseline (beating that, not a coin flip, is what earns
"skilled").
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional

from .claims import ENRICHMENT, Claim, ClaimStore
from .grounding import (
    Action,
    CatalogContext,
    SEV_HARMFUL,
    ground_event,
    parse_action,
)
from .observability import OK, WRITE, ObservationEvent
from .skill import skill_report
from .workkinds import (
    KIND_COLUMN_DOC,
    KIND_DOMAIN,
    KIND_OWNER,
    KIND_PII,
    KIND_TABLE_DOC,
    KIND_TERM,
    score_state,
    scoreable,
)

SEP = "::"
IMPLICIT_CONFIDENCE = 0.6

# One thing a write can assert: (entity urn, work kind, column). Column is None
# for kinds that attach to the dataset rather than a field.
Surface = tuple[str, str, Optional[str]]


@dataclass
class GradedWrite:
    agent_id: str
    work_kind: str
    entity_urn: str
    column: Optional[str]
    correct: Optional[bool]  # True accept, False revert, None not gradeable
    ts: float
    rewrite: bool = False  # the surface already held metadata, so not new work


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


def event_work_kinds(event: ObservationEvent) -> set[str]:
    """Which kinds of work one observed write asserts.

    Needed without a catalog in hand: conduct is recorded for actions that were
    blocked before they could be grounded, and for kinds nothing here can settle.
    """
    if event.op != WRITE:
        return set()
    action = parse_action(event)
    if action.tool.startswith("remove_") or action.operation == "remove":
        return set()
    return {kind for kind, _ in _targets(action)}


def _surfaces(action: Action) -> list[Surface]:
    urn = action.entity_urn
    if not urn:
        return []
    return [(urn, kind, column) for kind, column in _targets(action)]


class SurfaceLedger:
    """Which artifact surfaces already held metadata when the day began.

    Occupancy is fixed as of the start of the tick, and that choice is the whole
    design. Judged continuously, the first agent of the day to reach a column
    would consume it and every agent cast after would find nothing left to be
    scored on, so trust would track cast order instead of skill. Judged as of the
    start of the day, every agent in a tick answers the same question from the
    same state, which is what makes the leaderboard a comparison rather than a
    race, while a column documented yesterday is occupied for everyone today.

    Occupancy has two sources that do not overlap. The catalog carries what it
    shipped with, read per surface through the context, so a mutation that blanks
    a description hands that surface back as new work. This ledger carries what
    agents have written since. A write that was blocked or held never reached
    DataHub, so it leaves the surface new for whoever comes next; a removal frees
    the surface it emptied.

    Within a tick the ledger also remembers which (agent, surface) pairs it has
    already graded, so one agent writing the same column twice in a day is scored
    once. Two different agents writing it are both scored: that is the comparison.
    """

    def __init__(self, filled: Optional[Iterable[Surface]] = None):
        self.filled: set[Surface] = set(filled or ())
        self.graded: set[tuple[str, Surface]] = set()

    @classmethod
    def as_of(
        cls, events: Iterable[ObservationEvent], before_ts: Optional[float] = None
    ) -> "SurfaceLedger":
        """Occupancy implied by the writes that landed before `before_ts`."""
        ledger = cls()
        for event in events:
            if before_ts is not None and event.ts >= before_ts:
                continue
            ledger.absorb(event)
        return ledger

    def absorb(self, event: ObservationEvent) -> None:
        """Apply one observed write to occupancy. Only landed writes count."""
        if event.op != WRITE or event.status != OK:
            return
        action = parse_action(event)
        removal = action.tool.startswith("remove_") or action.operation == "remove"
        if action.tool.startswith("remove_"):
            # a removal empties the same surface its add-shaped twin would fill,
            # and only the add-shaped name is in the target map
            action = replace(action, tool="add_" + action.tool.removeprefix("remove_"))
        for surface in _surfaces(action):
            if removal:
                self.filled.discard(surface)
            else:
                self.filled.add(surface)

    def is_new(self, surface: Surface, agent_id: str) -> bool:
        return surface not in self.filled and (agent_id, surface) not in self.graded

    def mark_graded(self, surface: Surface, agent_id: str) -> None:
        self.graded.add((agent_id, surface))


def _catalog_filled(
    kind: str, dataset: str, column: Optional[str], ctx: CatalogContext
) -> bool:
    """Did the catalog itself already carry metadata of this kind here?

    Only column documentation ships pre-filled: the generator documents some
    columns and leaves others as enricher targets. Nothing arrives pre-tagged,
    pre-owned or pre-termed, so for every other kind the observation log is the
    whole story.
    """
    if kind == KIND_COLUMN_DOC and column:
        return bool((ctx.column_description(dataset, column) or "").strip())
    return False


def _gradeable(kind: str, dataset: str, column: Optional[str], ctx: CatalogContext) -> bool:
    """Is there catalog truth to judge a clean write of this kind against?

    Only asked for kinds this deployment can settle at all; see _scoreable_here.
    """
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


def graded_targets(
    event: ObservationEvent,
    ctx: CatalogContext,
    ledger: Optional[SurfaceLedger] = None,
) -> list[GradedWrite]:
    """Grade each thing a write asserts, consistent with the A2 findings.

    Pass a shared `ledger` when grading a whole tick so that work on artifacts
    that were not new is left unscored. Grading records the (agent, surface) pairs
    it consumed. With no ledger every surface is treated as new, which is what a
    single-event caller wants.
    """
    ledger = SurfaceLedger() if ledger is None else ledger
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
        surface = (action.entity_urn, kind, column)
        rewrite = False
        if not scoreable(kind)[0]:
            # No settlement source for this kind here, so nothing about it is
            # scored. Grading only its violations would let an agent bank reverts
            # it can never offset with accepts, which looks like a trust score
            # but is really an artifact of what we happen to be able to check.
            # The write is still observed, still grounded, still governed.
            correct: Optional[bool] = None
        elif (_catalog_filled(kind, dataset, column, ctx)
              or not ledger.is_new(surface, event.agent_id)):
            # The surface already held metadata of this kind, so the agent is not
            # working a new artifact. Graded either way the number would lie: an
            # accept credits an answer the agent did not have to derive, a revert
            # punishes it for a world that has run out of virgin work, and either
            # one counted daily turns a single judgment call into a pile of
            # evidence. Not scored, still observed and still grounded.
            correct = None
            rewrite = True
        elif any(f.severity == SEV_HARMFUL for f in fs):
            correct = False                        # a grounded violation
        elif fs:
            correct = False                        # a warn (e.g. low quality) still fails
        elif _gradeable(kind, dataset, column, ctx):
            correct = True                         # clean write on a judgeable surface
        else:
            correct = None                         # nothing to grade against
        if correct is not None:
            ledger.mark_graded(surface, event.agent_id)
        out.append(GradedWrite(event.agent_id, kind, action.entity_urn, column,
                               correct, event.ts, rewrite=rewrite))
    return out


def composite_id(agent_id: str, work_kind: str) -> str:
    return f"{agent_id}{SEP}{work_kind}"


def split_id(composite: str) -> tuple[str, str]:
    agent, _, kind = composite.partition(SEP)
    return agent, kind


def settle_observations(
    events: list[ObservationEvent],
    ctx: CatalogContext,
    store: ClaimStore,
    ledger: Optional[SurfaceLedger] = None,
) -> dict[str, int]:
    """Record and settle a claim per gradeable write, keyed by agent x kind.

    `ledger` says which surfaces already held metadata when the day began; build
    it with SurfaceLedger.as_of over the prior observation history so work on
    artifacts that were not new goes unscored. Omitted, only the catalog's own
    state counts, which is what a single-tick caller with no history wants.

    `rewrite` in the returned counts is the subset of `unsettled` that went
    unscored for that reason. It is the diagnostic worth watching: a world whose
    rewrite count climbs while accepts fall is a world that has run out of new
    work, not a roster that got worse.
    """
    ledger = SurfaceLedger() if ledger is None else ledger
    counts = {"recorded": 0, "settled": 0, "accepted": 0, "reverted": 0,
              "unsettled": 0, "rewrite": 0}
    for event in events:
        for gw in graded_targets(event, ctx, ledger):
            claim = Claim(
                agent_id=composite_id(gw.agent_id, gw.work_kind),
                model_id="observed",
                claim_type=ENRICHMENT,
                entity_urn=gw.entity_urn,
                prediction={"kind": gw.work_kind, "column": gw.column,
                            "agent": gw.agent_id, "rewrite": gw.rewrite},
                confidence=IMPLICIT_CONFIDENCE,
                evidence=["observed-write"],
                created_ts=gw.ts,
            )
            store.record(claim)
            counts["recorded"] += 1
            if gw.correct is None:
                counts["unsettled"] += 1
                counts["rewrite"] += int(gw.rewrite)
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
        out.setdefault(agent, {})[kind] = _with_score_state(kind, rec, **kwargs)
    return out


def _with_score_state(kind: str, rec: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Attach the honest state of this score, and why, to a skill record."""
    state, reason = score_state(
        kind, rec.get("n_settled", 0),
        **({"min_settled": kwargs["min_settled"]} if "min_settled" in kwargs else {}),
    )
    return {**rec, "score_state": state, "score_reason": reason}


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
        state, reason = score_state(kind, rec["n_settled"])
        rows.append({
            "agent_id": agent,
            "work_kind": kind,
            "trust": rec["trust"],
            "verdict": rec["verdict"],
            "score_state": state,
            "score_reason": reason or None,
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
