"""Drive one roster agent through the gateway against a catalog.

A roster agent is a stable identity plus a work kind and a competence profile.
This turns it into the concrete writer agent for its kind, wired with the profile
system prompt, points it at the catalog's datasets, and applies each proposal as
a real tool call through the gateway (where it is observed, grounded, and, under
enforce, possibly held or blocked). The result is a small stat the tick logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agents.enricher import EnricherAgent
from .agents.piitagger import PiiTaggerAgent
from .governance import apply_claim
from .roster import KIND_COLUMN_DOC, KIND_PII, RosterAgent, profile_system


def build_agent(ragent: RosterAgent, mcp: Any, llm: Any) -> Any:
    """The concrete writer agent for a roster entry, under its profile prompt."""
    if ragent.work_kind == KIND_COLUMN_DOC:
        cls: Any = EnricherAgent
    elif ragent.work_kind == KIND_PII:
        cls = PiiTaggerAgent
    else:
        raise ValueError(f"no writer agent wired for work kind {ragent.work_kind!r}")
    system = profile_system(ragent.work_kind, ragent.profile)
    return cls(mcp, llm, agent_id=ragent.agent_id, system=system)


@dataclass
class RunStat:
    agent_id: str
    work_kind: str
    profile: str
    proposed: int = 0
    applied: int = 0
    blocked: int = 0


def run_roster_agent(
    ragent: RosterAgent, mcp: Any, llm: Any, dataset_urns: list[str]
) -> RunStat:
    """Propose over each dataset and apply through the gateway. Never raises."""
    agent = build_agent(ragent, mcp, llm)
    stat = RunStat(ragent.agent_id, ragent.work_kind, ragent.profile)
    for urn in dataset_urns:
        try:
            claims = agent.propose(urn)
        except Exception:
            continue  # an LLM or read hiccup on one dataset should not stop the rest
        for claim in claims:
            stat.proposed += 1
            try:
                apply_claim(mcp, claim)
                stat.applied += 1
            except Exception:
                # forwarded but errored downstream, or held/blocked under enforce;
                # either way the gateway recorded the observation
                stat.blocked += 1
    return stat
