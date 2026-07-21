"""Compressed ground-truth timeline: what happens to the world, tick by tick.

One tick = one simulated hour. The default scenario runs four simulated days
in seconds of wall time, which is what makes settlement demonstrable in a
short demo and deterministic for tests (fixed seed).

Happenings are the world-side script (schema changes, late loads, incidents).
The runner turns them into agent observations and, later, into ground-truth
events for the settlement engine. Truth is derived from the world itself
(blast sets from column lineage, SLA outcomes from the lateness plan), never
hand-labelled, so scenario edits cannot drift out of sync with settlement.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from .world import World

TICK_SECONDS = 3600.0
BASE_TS = 1_700_000_000.0

# Happening kinds
SCHEMA_CHANGE = "schema_change"  # payload: dataset, dropped_column
INCIDENT_OPEN = "incident_open"  # payload: dataset, incident_urn, root_cause
INCIDENT_CLOSE = "incident_close"
LOAD_ARRIVED = "load_arrived"  # payload: dataset, late (bool)
ENRICHMENT_PASS = "enrichment_pass"  # enricher agents run once


def ts(tick: int) -> float:
    return BASE_TS + tick * TICK_SECONDS


@dataclass(frozen=True)
class Happening:
    tick: int
    kind: str
    payload: dict


@dataclass
class Timeline:
    n_ticks: int
    happenings: list[Happening] = field(default_factory=list)
    # (dataset, day) -> load arrives after sla_hour that day
    lateness: dict[tuple[str, int], bool] = field(default_factory=dict)
    review_delay: int = 6  # ticks between an enrichment claim and steward review

    def at(self, tick: int) -> list[Happening]:
        return [h for h in self.happenings if h.tick == tick]


def build_default_timeline(world: World, seed: int = 11, n_days: int = 4) -> Timeline:
    """Four simulated days: loads with a planted lateness pattern, two schema
    changes with different blast shapes, two incidents with known root causes.
    """
    rng = random.Random(seed)
    n_ticks = n_days * 24
    tl = Timeline(n_ticks=n_ticks)

    raw = [d for d in world.datasets.values() if d.landing_hour is not None]

    # Load arrivals. raw_payments degrades from day 1: a planted pattern the
    # sentinel can genuinely learn from history rather than guess.
    for day in range(n_days):
        for d in raw:
            if d.name == "raw_payments":
                late = day >= 1  # on time day 0, late every day after
            elif d.name == "raw_web_events":
                late = day == 2  # single surprise miss, hard to foresee
            else:
                late = rng.random() < 0.08  # background noise
            tl.lateness[(d.name, day)] = late
            arrival_hour = (d.sla_hour or 0) + 2 if late else d.landing_hour or 0
            tl.happenings.append(
                Happening(
                    tick=day * 24 + arrival_hour,
                    kind=LOAD_ARRIVED,
                    payload={"dataset": d.name, "late": late, "day": day},
                )
            )

    # Enrichment pass early on day 0.
    tl.happenings.append(Happening(tick=1, kind=ENRICHMENT_PASS, payload={}))

    # Schema change 1 (day 0, hour 20): drop raw_orders.discount_code.
    # Blast: stg_orders, fct_orders. NOT fct_revenue/rpt (they read total only).
    tl.happenings.append(
        Happening(
            tick=20,
            kind=SCHEMA_CHANGE,
            payload={"dataset": "raw_orders", "dropped_column": "discount_code"},
        )
    )

    # Incident (day 1, hour 17): fct_revenue stale; true root cause is the
    # late raw_payments feed, one of several upstream candidates.
    inc1 = "urn:li:incident:fct_revenue_stale_d1"
    tl.happenings.append(
        Happening(
            tick=41,
            kind=INCIDENT_OPEN,
            payload={
                "dataset": "fct_revenue",
                "incident_urn": inc1,
                "root_cause": "raw_payments",
            },
        )
    )
    tl.happenings.append(
        Happening(tick=45, kind=INCIDENT_CLOSE, payload={"incident_urn": inc1})
    )

    # Schema change 2 (day 2, hour 12): drop raw_customers.email.
    # Blast: stg_customers, dim_customers.
    tl.happenings.append(
        Happening(
            tick=60,
            kind=SCHEMA_CHANGE,
            payload={"dataset": "raw_customers", "dropped_column": "email"},
        )
    )

    # Incident 2 (day 2, hour 22): rpt_daily_kpis wrong; root cause the
    # surprise raw_web_events miss.
    inc2 = "urn:li:incident:rpt_kpis_wrong_d2"
    tl.happenings.append(
        Happening(
            tick=70,
            kind=INCIDENT_OPEN,
            payload={
                "dataset": "rpt_daily_kpis",
                "incident_urn": inc2,
                "root_cause": "raw_web_events",
            },
        )
    )
    tl.happenings.append(
        Happening(tick=74, kind=INCIDENT_CLOSE, payload={"incident_urn": inc2})
    )

    tl.happenings.sort(key=lambda h: h.tick)
    return tl


def sla_outcome_ticks(world: World, tl: Timeline) -> list[tuple[int, str, bool]]:
    """(tick, dataset, missed) for every raw dataset and simulated day."""
    out = []
    for (name, day), late in sorted(tl.lateness.items()):
        d = world.datasets[name]
        out.append((day * 24 + (d.sla_hour or 0), name, late))
    return out


def forecast_ticks(world: World, tl: Timeline) -> list[tuple[int, str, int]]:
    """(tick, dataset, day): when the sentinel is asked to forecast the
    upcoming SLA window, three hours before each deadline."""
    out = []
    for (name, day), _ in sorted(tl.lateness.items()):
        d = world.datasets[name]
        tick = day * 24 + (d.sla_hour or 0) - 3
        if tick >= 0:
            out.append((tick, name, day))
    return out


def observed_history(
    tl: Timeline, dataset: str, before_day: int
) -> list[bool]:
    """Lateness the sentinel can legitimately see: prior days only."""
    return [
        late
        for (name, day), late in sorted(tl.lateness.items())
        if name == dataset and day < before_day
    ]
