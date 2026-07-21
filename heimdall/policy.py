"""Policy decisions: the control-plane half of the gateway.

Given who is writing (their settled track record) and what they are writing
(the action, grounded against the catalog right now), decide whether to let it
through, hold it for a steward, or block it outright. The decision combines two
signals no generic proxy has together:

  - the author's trust and skill-vs-luck verdict per the settled ledger (B1)
  - the catalog-grounded findings for this specific action (A2)

That second signal is why a catalog-violating write is stopped in flight, even
from an agent with an otherwise clean record: the action itself is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .grounding import SEV_HARMFUL, SEV_WARN
from .skill import HARMFUL as VERDICT_HARMFUL

TIER_ACCEPT = "accept"   # trusted author, clean action: auto-accept
TIER_PASS = "pass"       # forward normally, annotate
TIER_HOLD = "hold"       # suspicious: intercept, do not apply, queue for review
TIER_BLOCK = "block"     # harmful: reject before it reaches the catalog


@dataclass
class PolicyThresholds:
    accept_at: float = 70.0   # trust at or above this, with a clean action, auto-accepts
    hold_floor: float = 55.0  # a proven author below this has clean writes held
    min_settled: int = 5      # settled claims before trust gates apply


@dataclass
class PolicyDecision:
    tier: str
    reason: str = ""
    findings: list[Any] = field(default_factory=list)

    @property
    def forwards(self) -> bool:
        return self.tier in (TIER_ACCEPT, TIER_PASS)


def decide(
    *,
    agent_trust: float,
    agent_verdict: str,
    n_settled: int,
    findings: list[Any],
    min_trust: float = 0.0,
    thresholds: PolicyThresholds | None = None,
) -> PolicyDecision:
    """Accept, pass, hold, or block, from author standing and action findings."""
    th = thresholds or PolicyThresholds()
    harmful = [f for f in findings if getattr(f, "severity", None) == SEV_HARMFUL]
    warn = [f for f in findings if getattr(f, "severity", None) == SEV_WARN]

    # 1. the action itself violates the catalog: block, whoever sent it
    if harmful:
        return PolicyDecision(TIER_BLOCK, harmful[0].reason, findings)

    # 2. the author has a worse-than-chance record: block
    if agent_verdict == VERDICT_HARMFUL:
        return PolicyDecision(
            TIER_BLOCK,
            f"heimdall policy: author record is worse than chance over "
            f"{n_settled} settled claims; write blocked",
            findings,
        )

    # 3. a hard trust floor (when configured) blocks below it
    if min_trust > 0 and agent_trust < min_trust:
        return PolicyDecision(
            TIER_BLOCK,
            f"heimdall policy: author trust {agent_trust:.1f} is below the floor "
            f"{min_trust:.1f}; write blocked. Reads are still allowed.",
            findings,
        )

    # 4. the action is questionable (a warn finding): hold for review
    if warn:
        return PolicyDecision(TIER_HOLD, warn[0].reason, findings)

    proven = n_settled >= th.min_settled
    # 5. a proven-but-mediocre author has clean writes held
    if proven and agent_trust < th.hold_floor:
        return PolicyDecision(
            TIER_HOLD,
            f"heimdall policy: author trust {agent_trust:.1f} is below the review "
            f"floor {th.hold_floor:.1f}; write held for a steward",
            findings,
        )
    # 6. a proven, trusted author writing cleanly is auto-accepted
    if proven and agent_trust >= th.accept_at:
        return PolicyDecision(
            TIER_ACCEPT,
            f"heimdall policy: trusted author (trust {agent_trust:.1f}); auto-accepted",
            findings,
        )
    # 7. otherwise forward and annotate (e.g. an unproven author acting cleanly)
    return PolicyDecision(TIER_PASS, "", findings)
