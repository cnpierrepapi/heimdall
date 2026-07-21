"""Replay a timeline against a set of agents and settle everything.

The runner is the only component that sees scenario truth. Agents receive
observations (a schema change happened, an incident opened, here is the
lateness history you could legitimately have seen) and answer with
predictions; the runner derives ground truth from the world itself and feeds
it to the settlement engine ticks later.

The stub agents at the bottom exist to validate the pipeline end to end:
a truth-peeking agent must come out `skilled`, a guesser must not. The real
LLM agents implement the same protocol against a live DataHub instance.
"""

from __future__ import annotations

import random
from typing import Any, Optional, Protocol

from ..claims import (
    BLAST_RADIUS,
    ENRICHMENT,
    FRESHNESS_SLA,
    ROOT_CAUSE,
    Claim,
    ClaimStore,
)
from ..settle import (
    ASSERTION_RESULT,
    INCIDENT_RESOLVED,
    SLA_OUTCOME,
    STEWARD_REVIEW,
    VERDICT_ACCEPTED,
    VERDICT_REVERTED,
    GroundTruthEvent,
    SettlementEngine,
)
from ..skill import skill_report
from .timeline import (
    ENRICHMENT_PASS,
    INCIDENT_CLOSE,
    INCIDENT_OPEN,
    SCHEMA_CHANGE,
    Timeline,
    forecast_ticks,
    observed_history,
    sla_outcome_ticks,
    ts,
)
from .world import World, dataset_urn

TRUTH_DELAY_TICKS = 2  # next pipeline build surfaces assertion results


class Agent(Protocol):
    agent_id: str

    def on_schema_change(
        self, world: World, dataset: str, dropped_column: str, candidates: list[str]
    ) -> list[tuple[str, bool, float]]:
        """Per candidate: (candidate dataset name, will_break, confidence)."""
        ...

    def on_incident(
        self, world: World, incident_urn: str, dataset: str, candidates: list[str]
    ) -> tuple[str, float]:
        """(root cause dataset name, confidence)."""
        ...

    def on_sla_forecast(
        self, world: World, dataset: str, history: list[bool]
    ) -> tuple[bool, float]:
        """(will_miss_sla, confidence)."""
        ...

    def on_enrichment(
        self, world: World, dataset: str, column: str
    ) -> tuple[str, float]:
        """(proposed description, confidence)."""
        ...


def normalize_directional(statement: bool, confidence: float) -> tuple[bool, float]:
    """State the direction actually believed: conf < 0.5 flips the statement."""
    if confidence < 0.5:
        return (not statement), 1.0 - confidence
    return statement, confidence


def run(
    world: World,
    tl: Timeline,
    agents: list[Agent],
    store: ClaimStore,
    model_id: str = "stub",
    n_sims: int = 10_000,
) -> dict[str, Any]:
    engine = SettlementEngine(store)
    review_keyword = _steward_verdict_fn(world)

    # Pre-compute per-tick schedules.
    forecasts = forecast_ticks(world, tl)
    sla_truths = sla_outcome_ticks(world, tl)
    pending: list[tuple[int, GroundTruthEvent]] = [
        (
            tick,
            GroundTruthEvent(
                event_type=SLA_OUTCOME,
                entity_urn=dataset_urn(name),
                payload={"missed": missed},
                ts=ts(tick),
            ),
        )
        for tick, name, missed in sla_truths
    ]
    open_incidents: dict[str, dict[str, Any]] = {}

    for tick in range(tl.n_ticks + TRUTH_DELAY_TICKS + tl.review_delay + 1):
        # 1. deliver any ground truth scheduled for this tick
        for due, event in [p for p in pending if p[0] == tick]:
            engine.process_event(event)
        pending = [p for p in pending if p[0] != tick]

        # 2. agent-facing happenings
        for h in tl.at(tick):
            if h.kind == SCHEMA_CHANGE:
                dataset = h.payload["dataset"]
                dropped = h.payload["dropped_column"]
                candidates = sorted(world.descendants(dataset))
                blast = world.blast_set(dataset, dropped)
                for agent in agents:
                    if not hasattr(agent, "on_schema_change"):
                        continue
                    for cand, will_break, conf in agent.on_schema_change(
                        world, dataset, dropped, candidates
                    ):
                        will_break, conf = normalize_directional(will_break, conf)
                        store.record(
                            Claim(
                                agent_id=agent.agent_id,
                                model_id=model_id,
                                claim_type=BLAST_RADIUS,
                                entity_urn=dataset_urn(cand),
                                prediction={
                                    "will_break": will_break,
                                    "changed_dataset": dataset,
                                    "dropped_column": dropped,
                                },
                                confidence=conf,
                                created_ts=ts(tick),
                            )
                        )
                # truth: assertion per candidate at the next build
                for cand in candidates:
                    pending.append(
                        (
                            tick + TRUTH_DELAY_TICKS,
                            GroundTruthEvent(
                                event_type=ASSERTION_RESULT,
                                entity_urn=dataset_urn(cand),
                                payload={"passed": cand not in blast},
                                ts=ts(tick + TRUTH_DELAY_TICKS),
                            ),
                        )
                    )

            elif h.kind == INCIDENT_OPEN:
                dataset = h.payload["dataset"]
                incident_urn = h.payload["incident_urn"]
                candidates = sorted(world.ancestors(dataset))
                open_incidents[incident_urn] = h.payload
                for agent in agents:
                    if not hasattr(agent, "on_incident"):
                        continue
                    pick, conf = agent.on_incident(
                        world, incident_urn, dataset, candidates
                    )
                    store.record(
                        Claim(
                            agent_id=agent.agent_id,
                            model_id=model_id,
                            claim_type=ROOT_CAUSE,
                            entity_urn=incident_urn,
                            prediction={
                                "root_cause_urn": dataset_urn(pick),
                                "n_candidates": len(candidates),
                            },
                            confidence=conf,
                            created_ts=ts(tick),
                        )
                    )

            elif h.kind == INCIDENT_CLOSE:
                info = open_incidents.pop(h.payload["incident_urn"])
                engine.process_event(
                    GroundTruthEvent(
                        event_type=INCIDENT_RESOLVED,
                        entity_urn=h.payload["incident_urn"],
                        payload={"root_cause_urn": dataset_urn(info["root_cause"])},
                        ts=ts(tick),
                    )
                )

            elif h.kind == ENRICHMENT_PASS:
                for dataset_obj, column in world.undocumented():
                    for agent in agents:
                        if not hasattr(agent, "on_enrichment"):
                            continue
                        description, conf = agent.on_enrichment(
                            world, dataset_obj.name, column.name
                        )
                        claim = store.record(
                            Claim(
                                agent_id=agent.agent_id,
                                model_id=model_id,
                                claim_type=ENRICHMENT,
                                entity_urn=dataset_obj.urn,
                                prediction={
                                    "column": column.name,
                                    "description": description,
                                },
                                confidence=conf,
                                created_ts=ts(tick),
                            )
                        )
                        verdict = review_keyword(dataset_obj.name, column.name, description)
                        pending.append(
                            (
                                tick + tl.review_delay,
                                GroundTruthEvent(
                                    event_type=STEWARD_REVIEW,
                                    entity_urn=dataset_obj.urn,
                                    payload={
                                        "column": column.name,
                                        "verdict": verdict,
                                        "claim_id": claim.claim_id,
                                    },
                                    ts=ts(tick + tl.review_delay),
                                ),
                            )
                        )

        # 3. SLA forecasts due this tick
        for f_tick, dataset, day in forecasts:
            if f_tick != tick:
                continue
            history = observed_history(tl, dataset, before_day=day)
            for agent in agents:
                if not hasattr(agent, "on_sla_forecast"):
                    continue
                will_miss, conf = agent.on_sla_forecast(world, dataset, history)
                will_miss, conf = normalize_directional(will_miss, conf)
                store.record(
                    Claim(
                        agent_id=agent.agent_id,
                        model_id=model_id,
                        claim_type=FRESHNESS_SLA,
                        entity_urn=dataset_urn(dataset),
                        prediction={"will_miss_sla": will_miss, "day": day},
                        confidence=conf,
                        created_ts=ts(tick),
                    )
                )

    report = skill_report(store, n_sims=n_sims)
    engine.close()
    return {
        "n_claims": len(store.claims()),
        "n_unsettled": len(store.unsettled()),
        "report": report,
    }


def _steward_verdict_fn(world: World):
    keywords = {
        (d.name, c.name): c.gold_keywords
        for d in world.datasets.values()
        for c in d.columns
    }

    def verdict(dataset: str, column: str, description: str) -> str:
        golds = keywords.get((dataset, column), ())
        text = description.lower()
        if golds and any(k in text for k in golds):
            return VERDICT_ACCEPTED
        return VERDICT_REVERTED

    return verdict


# -- stub agents (pipeline validation only) ----------------------------------


class RandomAgent:
    """Guesses everything, states everything with the same high confidence.

    Deliberately overconfident: its calibration curve is the demo's picture
    of what an untrustworthy agent looks like.
    """

    def __init__(self, agent_id: str = "random-agent", seed: int = 99):
        self.agent_id = agent_id
        self.rng = random.Random(seed)

    def on_schema_change(self, world, dataset, dropped_column, candidates):
        return [(c, self.rng.random() < 0.5, 0.8) for c in candidates]

    def on_incident(self, world, incident_urn, dataset, candidates):
        return self.rng.choice(candidates), 0.8

    def on_sla_forecast(self, world, dataset, history):
        return self.rng.random() < 0.5, 0.8

    def on_enrichment(self, world, dataset, column):
        if self.rng.random() < 0.3:
            d = world.datasets[dataset]
            col = next(c for c in d.columns if c.name == column)
            if col.gold_keywords:
                return f"Field capturing the {col.gold_keywords[0]}.", 0.8
        return "Auto-generated placeholder description.", 0.8


class SharpAgent:
    """Peeks at scenario truth with the given accuracy. Test harness only."""

    def __init__(
        self,
        tl: Timeline,
        agent_id: str = "sharp-agent",
        accuracy: float = 0.9,
        seed: int = 42,
    ):
        self.tl = tl
        self.agent_id = agent_id
        self.accuracy = accuracy
        self.rng = random.Random(seed)

    def _peek(self, truth: bool) -> bool:
        return truth if self.rng.random() < self.accuracy else not truth

    def on_schema_change(self, world, dataset, dropped_column, candidates):
        blast = world.blast_set(dataset, dropped_column)
        return [
            (c, self._peek(c in blast), self.accuracy) for c in candidates
        ]

    def on_incident(self, world, incident_urn, dataset, candidates):
        true_root = next(
            h.payload["root_cause"]
            for h in self.tl.happenings
            if h.kind == INCIDENT_OPEN and h.payload["incident_urn"] == incident_urn
        )
        if self.rng.random() < self.accuracy:
            return true_root, 0.75
        others = [c for c in candidates if c != true_root] or candidates
        return self.rng.choice(others), 0.75

    def on_sla_forecast(self, world, dataset, history):
        # find the day being forecast: first day with no observed history yet
        day = len(history)
        truth = self.tl.lateness.get((dataset, day), False)
        return self._peek(truth), self.accuracy

    def on_enrichment(self, world, dataset, column):
        d = world.datasets[dataset]
        col = next(c for c in d.columns if c.name == column)
        if col.gold_keywords and self.rng.random() < self.accuracy:
            return (
                f"{column.replace('_', ' ').capitalize()}: {col.gold_keywords[0]} value.",
                0.85,
            )
        return "Miscellaneous attribute.", 0.85
