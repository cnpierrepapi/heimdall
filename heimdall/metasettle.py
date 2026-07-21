"""Settlement for meta claims: claims about other claims settle off the ledger.

A revert forecast or an arbitration pick has its ground truth produced by the
same steward events that settle the underlying proposal, so once the target
claim settles, the meta claim's outcome is mechanical. This module performs
that propagation; it never settles a meta claim whose target is still open.

Tamper audits are not handled here: their ground truth is the definitive
account of what a rogue session touched, which the orchestrator (demo) or a
provenance sweep (production) supplies directly.
"""

from __future__ import annotations

from .claims import ARBITRATION, REVERT_FORECAST, ClaimStore


def settle_meta_claims(store: ClaimStore) -> int:
    """Settle open revert forecasts and arbitrations with settled targets."""
    settled = 0

    for claim in store.claims(claim_type=REVERT_FORECAST, settled=False):
        target = store.get(str(claim.prediction.get("target_claim_id")))
        if target is None or not target.settled:
            continue
        reverted = not target.correct
        correct = bool(claim.prediction.get("will_be_reverted")) == reverted
        store.settle(
            claim.claim_id,
            outcome={"target_claim_id": target.claim_id, "target_reverted": reverted},
            correct=correct,
            settled_ts=target.settled_ts,
        )
        settled += 1

    for claim in store.claims(claim_type=ARBITRATION, settled=False):
        winner = store.get(str(claim.prediction.get("winner_claim_id")))
        if winner is None or not winner.settled:
            continue
        store.settle(
            claim.claim_id,
            outcome={
                "winner_claim_id": winner.claim_id,
                "winner_accepted": bool(winner.correct),
            },
            correct=bool(winner.correct),
            settled_ts=winner.settled_ts,
        )
        settled += 1

    return settled
