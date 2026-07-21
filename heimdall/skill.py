"""Skill-vs-luck decomposition and trust scores.

A leaderboard of raw win rates is misleading: with few settled claims, a
lucky agent is indistinguishable from a good one. This module asks, per
agent, whether the observed record could plausibly have been produced by an
agent with no information at all.

Method: for each settled claim we define the null win probability, i.e. the
chance a no-skill agent would have gotten that claim right:

- directional binary claims (blast radius, freshness): 0.5 by symmetry
- root cause: 1 / n_candidates, where the claim records how many upstream
  candidates existed when the pick was made
- enrichment: the pooled acceptance rate across ALL agents' settled
  enrichment claims (how often stewards accept a machine description at all)

The null win total is then a Poisson binomial; we estimate the tail by Monte
Carlo, compute one-sided p-values per agent, and control the false discovery
rate across agents with Benjamini-Hochberg. Only agents surviving the FDR
gate are called skilled; a symmetric lower-tail test flags agents doing
significantly worse than chance.

The trust score shown to consumers is deliberately conservative: Brier-based
quality shrunk toward the neutral 0.5 by settled-claim count, so a lucky
3-for-3 agent does not outrank a proven 80-for-100 one.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from .claims import ENRICHMENT, ROOT_CAUSE, Claim, ClaimStore
from .settle import agent_stats, brier

FDR_LEVEL = 0.10
N_SIMS = 10_000
SHRINKAGE_K = 20  # settled claims needed for ~half weight on observed record

SKILLED = "skilled"
LUCK = "not distinguishable from luck"
HARMFUL = "worse than chance"
UNSETTLED = "insufficient settled claims"


def null_probability(claim: Claim, pooled_enrichment_rate: float) -> float:
    """P(a no-skill agent gets this claim right)."""
    if claim.claim_type == ROOT_CAUSE:
        n_candidates = int(claim.prediction.get("n_candidates", 2))
        return 1.0 / max(2, n_candidates)
    if claim.claim_type == ENRICHMENT:
        return pooled_enrichment_rate
    return 0.5


def pooled_enrichment_acceptance(store: ClaimStore) -> float:
    settled = store.claims(claim_type=ENRICHMENT, settled=True)
    if not settled:
        return 0.5
    return sum(1 for c in settled if c.correct) / len(settled)


def skill_report(
    store: ClaimStore,
    n_sims: int = N_SIMS,
    fdr_level: float = FDR_LEVEL,
    seed: int = 7,
    min_settled: int = 5,
) -> dict[str, dict[str, Any]]:
    """Per-agent skill verdicts, p/q values, and trust scores."""
    rng = random.Random(seed)
    pooled_rate = pooled_enrichment_acceptance(store)
    stats = agent_stats(store)

    report: dict[str, dict[str, Any]] = {}
    p_values: dict[str, tuple[float, float]] = {}  # agent -> (upper, lower)

    for agent_id, entry in stats.items():
        settled = store.claims(agent_id=agent_id, settled=True)
        rec: dict[str, Any] = dict(entry)
        rec["trust"] = trust_score(settled)
        if len(settled) < min_settled:
            rec["verdict"] = UNSETTLED
            rec["p_value"] = None
            rec["q_value"] = None
            rec["expected_null_wins"] = None
            report[agent_id] = rec
            continue

        null_ps = [null_probability(c, pooled_rate) for c in settled]
        wins = rec["wins"]
        upper, lower = _mc_tail_p_values(null_ps, wins, n_sims, rng)
        rec["p_value"] = upper
        rec["p_value_lower"] = lower
        rec["expected_null_wins"] = sum(null_ps)
        p_values[agent_id] = (upper, lower)
        report[agent_id] = rec

    # BH-FDR on the upper tail (better than chance)
    skilled_ids = _benjamini_hochberg(
        {a: pv[0] for a, pv in p_values.items()}, fdr_level
    )
    # BH-FDR on the lower tail (worse than chance)
    harmful_ids = _benjamini_hochberg(
        {a: pv[1] for a, pv in p_values.items()}, fdr_level
    )

    for agent_id, (upper, _) in p_values.items():
        rec = report[agent_id]
        rec["q_value"] = rec.get("q_value")  # filled below for tested agents
        if agent_id in skilled_ids:
            rec["verdict"] = SKILLED
        elif agent_id in harmful_ids:
            rec["verdict"] = HARMFUL
        else:
            rec["verdict"] = LUCK

    _attach_q_values(report, {a: pv[0] for a, pv in p_values.items()})
    return report


def trust_score(settled: list[Claim]) -> float:
    """0-100 score: Brier quality shrunk toward neutral 50 by sample size."""
    n = len(settled)
    if n == 0:
        return 50.0
    mean_brier = sum(brier(c) for c in settled) / n
    quality = 1.0 - mean_brier  # 1 = perfectly confident and right
    weight = n / (n + SHRINKAGE_K)
    return round(100.0 * (weight * quality + (1.0 - weight) * 0.5), 1)


def _mc_tail_p_values(
    null_ps: list[float], wins: int, n_sims: int, rng: random.Random
) -> tuple[float, float]:
    """Monte Carlo Poisson-binomial tails with add-one smoothing."""
    ge = 0  # null >= observed (upper tail: is the agent better than chance?)
    le = 0  # null <= observed (lower tail: is the agent worse than chance?)
    for _ in range(n_sims):
        null_wins = sum(1 for p in null_ps if rng.random() < p)
        if null_wins >= wins:
            ge += 1
        if null_wins <= wins:
            le += 1
    return (ge + 1) / (n_sims + 1), (le + 1) / (n_sims + 1)


def _benjamini_hochberg(p_values: dict[str, float], level: float) -> set[str]:
    if not p_values:
        return set()
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(ordered)
    cutoff_rank: Optional[int] = None
    for rank, (_, p) in enumerate(ordered, start=1):
        if p <= level * rank / m:
            cutoff_rank = rank
    if cutoff_rank is None:
        return set()
    return {agent for agent, _ in ordered[:cutoff_rank]}


def _attach_q_values(
    report: dict[str, dict[str, Any]], upper_ps: dict[str, float]
) -> None:
    """BH-adjusted q-values for display (monotone step-up)."""
    if not upper_ps:
        return
    ordered = sorted(upper_ps.items(), key=lambda kv: kv[1])
    m = len(ordered)
    q_prev = 1.0
    adjusted: dict[str, float] = {}
    for rank in range(m, 0, -1):
        agent, p = ordered[rank - 1]
        q = min(q_prev, p * m / rank)
        adjusted[agent] = q
        q_prev = q
    for agent, q in adjusted.items():
        report[agent]["q_value"] = round(q, 4)
