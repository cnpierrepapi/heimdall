"""What an agent did, as distinct from how well it did it.

Trust answers whether an agent is better than chance, and it can only be computed
where something settles the claim. Conduct answers a different question that needs
no settlement at all: did this agent stay inside the catalog it was given. How
many writes it attempted, how many the gateway blocked or held, how many drew a
grounded finding citing a specific catalog fact, how much of the catalog it
touched.

Every agent has a conduct record, including the ones whose work this deployment
cannot score. An agent proposing owners earns no trust here and never will, but
if it invents a team that does not exist, that is caught, cited, and counted.
Withholding the score is not the same as having nothing to report, and the
console needs something honest to show in place of a rank.

Conduct is bucketed per (agent, work_kind) so it lines up with the leaderboard's
own key. Findings carry the id of the action that drew them, so each one lands on
the kind of work that caused it rather than being smeared across the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .grounding import SEV_HARMFUL, SEV_WARN, Finding
from .observability import BLOCKED, HELD, OK, WRITE, ObservationEvent
from .trust import event_work_kinds


@dataclass
class Conduct:
    """One agent's record of behaviour for one kind of work."""

    agent_id: str
    work_kind: str
    actions: int = 0        # writes of this kind the agent attempted
    applied: int = 0        # ... that landed
    blocked: int = 0        # ... that the gateway refused outright
    held: int = 0           # ... that it held for review
    errored: int = 0        # ... that it forwarded but that failed downstream
    harmful: int = 0        # grounded findings of harmful severity
    warn: int = 0           # grounded findings of warn severity
    entities: set[str] = field(default_factory=set)

    @property
    def clean_rate(self) -> Optional[float]:
        """Share of attempts that drew no finding and were not stopped."""
        if not self.actions:
            return None
        bad = self.harmful + self.warn + self.blocked + self.held + self.errored
        return round(max(0.0, (self.actions - bad)) / self.actions, 3)

    def as_row(self) -> dict[str, Any]:
        return {
            "n_actions": self.actions,
            "n_applied": self.applied,
            "n_blocked": self.blocked,
            "n_held": self.held,
            "n_errored": self.errored,
            "n_harmful": self.harmful,
            "n_warn": self.warn,
            "n_entities": len(self.entities),
            "clean_rate": self.clean_rate,
        }


def _writes(events: Iterable[ObservationEvent]) -> list[ObservationEvent]:
    return [e for e in events if e.op == WRITE]


def conduct_by_kind(
    events: Iterable[ObservationEvent],
    findings: Iterable[Finding] = (),
) -> dict[tuple[str, str], Conduct]:
    """Behaviour per (agent, work_kind), from observations and grounded findings.

    Blocked and held actions count as attempts: refusing to let a write land is
    exactly the kind of thing the record should remember about an agent.
    """
    out: dict[tuple[str, str], Conduct] = {}
    # where each observed action landed, so a finding can be attributed to the
    # kind of work that drew it rather than to the agent as a whole
    by_event: dict[str, tuple[str, list[str]]] = {}

    for e in _writes(events):
        kinds = sorted(event_work_kinds(e))
        by_event[e.event_id] = (e.agent_id, kinds)
        for kind in kinds:
            c = out.setdefault((e.agent_id, kind), Conduct(e.agent_id, kind))
            c.actions += 1
            if e.status == BLOCKED:
                c.blocked += 1
            elif e.status == HELD:
                c.held += 1
            elif e.status == OK:
                c.applied += 1
            else:
                # forwarded, observed, then rejected downstream. Counting it as
                # applied would say the catalog changed when it did not.
                c.errored += 1
            c.entities.update(e.entities)

    for f in findings:
        agent, kinds = by_event.get(f.event_id or "", (f.agent_id, []))
        for kind in kinds or []:
            c = out.setdefault((agent, kind), Conduct(agent, kind))
            if f.severity == SEV_HARMFUL:
                c.harmful += 1
            elif f.severity == SEV_WARN:
                c.warn += 1
    return out


def conduct_rows(
    events: Iterable[ObservationEvent], findings: Iterable[Finding] = ()
) -> dict[tuple[str, str], dict[str, Any]]:
    """conduct_by_kind as plain row fragments, keyed for merging into hd_agents."""
    return {key: c.as_row() for key, c in conduct_by_kind(events, findings).items()}
