"""Build the scoreboard projections from the ledger.

One computation path for every publishing transport: the PostgREST
publisher script and any SQL-based loader consume the same rows, so the
public scoreboard can never drift from what the ledger says.
"""

from __future__ import annotations

from typing import Any

from .claims import ClaimStore
from .skill import skill_report


def build_rows(store: ClaimStore, n_sims: int = 10_000) -> dict[str, list[dict[str, Any]]]:
    report = skill_report(store, n_sims=n_sims)
    claims = store.claims()

    model_by_agent: dict[str, str] = {}
    for c in claims:
        model_by_agent.setdefault(c.agent_id, c.model_id)

    agent_rows = []
    calibration_rows = []
    for agent_id, rec in report.items():
        agent_rows.append(
            {
                "agent_id": agent_id,
                "model_id": model_by_agent.get(agent_id),
                "trust": rec["trust"],
                "verdict": rec["verdict"],
                "n_total": rec["n_total"],
                "n_settled": rec["n_settled"],
                "wins": rec.get("wins"),
                "win_rate": rec.get("win_rate"),
                "brier_mean": rec.get("brier_mean"),
                "ece": rec.get("ece"),
                "p_value": rec.get("p_value"),
                "q_value": rec.get("q_value"),
                "expected_null_wins": rec.get("expected_null_wins"),
            }
        )
        for b in rec.get("calibration") or []:
            calibration_rows.append(
                {
                    "agent_id": agent_id,
                    "bin_low": b["bin_low"],
                    "bin_high": b["bin_high"],
                    "n": b["n"],
                    "mean_confidence": b["mean_confidence"],
                    "frac_true": b["frac_true"],
                }
            )

    claim_rows = [
        {
            "claim_id": c.claim_id,
            "agent_id": c.agent_id,
            "model_id": c.model_id,
            "claim_type": c.claim_type,
            "entity_urn": c.entity_urn,
            "prediction": c.prediction,
            "confidence": c.confidence,
            "created_ts": c.created_ts,
            "settled_ts": c.settled_ts,
            "outcome": c.outcome,
            "correct": c.correct,
        }
        for c in claims
    ]

    return {
        "ll_agents": agent_rows,
        "ll_calibration": calibration_rows,
        "ll_claims": claim_rows,
    }
