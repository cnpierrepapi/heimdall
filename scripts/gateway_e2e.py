"""Live end-to-end proof of the trust gateway against the real stack.

Every step here runs the production path: a real MCP client connects to the
real gateway process, which spawns the real mcp-server-datahub against the
live DataHub instance. Nothing is faked; the only scripted element is the
steward's judgment, which in production is a human action and is performed
here as a real catalog revert.

The scenario is the product pitch end to end:

  1. the gateway mirrors the downstream tool surface exactly
  2. reads through the gateway carry trust context from the structured
     properties the writeback layer planted
  3. an uninstrumented agent's write is recorded as an implicit claim AND
     forwarded to the catalog for real
  4. a steward reverts the write (real update_description restoring the
     accepted text) and the implicit claim settles as wrong
  5. with policy=enforce and a trust floor, the same agent's next write is
     rejected before touching the catalog, no claim is recorded for it, and
     reads still work

Run on the box:
  MCP_SERVER_DATAHUB=~/dh/bin/mcp-server-datahub ~/dh/bin/python scripts/gateway_e2e.py
Exits non-zero if any check fails.
"""

from __future__ import annotations

import json
import os
import sys
import time

from heimdall.claims import ENRICHMENT, ClaimStore
from heimdall.mcp_client import DataHubMCP
from heimdall.settle import (
    STEWARD_REVIEW,
    VERDICT_REVERTED,
    GroundTruthEvent,
    SettlementEngine,
)
from heimdall.skill import trust_score
from heimdall.simulator.world import build_default_world

DB_PATH = os.environ.get("LEDGER_DB", os.path.expanduser("~/heimdall-demo.db"))
ROGUE = "rogue-agent"
ROGUE_TEXT = "Numeric field used internally by the ingestion job."
TRUST_FLOOR = 55.0

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"{status}  {name}" + (f" | {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def gateway_session(policy: str = "annotate", min_trust: float = 0.0) -> DataHubMCP:
    return DataHubMCP(
        command=sys.executable,
        args=["-m", "heimdall.gateway"],
        extra_env={
            "HEIMDALL_AGENT_ID": ROGUE,
            "LEDGER_DB": DB_PATH,
            "HEIMDALL_POLICY": policy,
            "HEIMDALL_MIN_TRUST": str(min_trust),
        },
    )


def main() -> None:
    world = build_default_world()
    target = world.datasets["raw_orders"]
    column = "order_total_usd"

    with ClaimStore(DB_PATH) as store:
        original = next(
            c.prediction["description"]
            for c in store.claims(claim_type=ENRICHMENT, settled=True)
            if c.entity_urn == target.urn
            and c.prediction.get("column") == column
            and c.correct
        )
        rogue_claims_before = len(store.claims(agent_id=ROGUE))

    with DataHubMCP() as plain:
        downstream_tools = {t.name for t in plain.list_tools()}

    # -- 1+2+3: annotate-mode session as an uninstrumented agent -------------
    with gateway_session() as gw:
        tools = {t.name for t in gw.list_tools()}
        check(
            "tool surface mirrored exactly",
            tools == downstream_tools,
            f"{len(tools)} tools",
        )

        text = gw.call("search", {"query": "lineworld", "num_results": 20})
        as_str = text if isinstance(text, str) else json.dumps(text)
        check(
            "reads carry trust context",
            "heimdall trust context" in as_str and "enricher-live" in as_str,
        )

        gw.call(
            "update_description",
            {
                "entity_urn": target.urn,
                "column_path": column,
                "description": ROGUE_TEXT,
                "operation": "replace",
            },
        )

    with ClaimStore(DB_PATH) as store:
        rogue_claims = store.claims(agent_id=ROGUE)
        new = [
            c
            for c in rogue_claims[rogue_claims_before:]
            if c.prediction.get("implicit")
        ]
        check(
            "implicit claim recorded on write",
            len(new) == 1 and new[0].claim_type == ENRICHMENT,
            f"confidence={new[0].confidence}" if new else "no claim found",
        )
        implicit = new[0] if new else None

    with DataHubMCP() as plain:
        snap = json.dumps(plain.get_entities([target.urn]))
        check("write forwarded to catalog", ROGUE_TEXT[:35] in snap)

        # -- 4: real steward action: revert to the accepted description ------
        plain.call(
            "update_description",
            {
                "entity_urn": target.urn,
                "column_path": column,
                "description": original,
                "operation": "replace",
            },
        )
        snap = json.dumps(plain.get_entities([target.urn]))
        check(
            "steward revert restored accepted text",
            original[:35] in snap and ROGUE_TEXT[:35] not in snap,
        )

    if implicit is not None:
        with ClaimStore(DB_PATH) as store:
            engine = SettlementEngine(store)
            engine.process_event(
                GroundTruthEvent(
                    event_type=STEWARD_REVIEW,
                    entity_urn=target.urn,
                    payload={
                        "column": column,
                        "verdict": VERDICT_REVERTED,
                        "claim_id": implicit.claim_id,
                    },
                    ts=time.time(),
                )
            )
            settled = store.get(implicit.claim_id)
            check(
                "implicit claim settled as wrong",
                settled is not None and settled.settled and settled.correct is False,
            )
            rogue_trust = trust_score(store.claims(agent_id=ROGUE, settled=True))
            print(f"      rogue-agent trust after revert: {rogue_trust}")
            check(
                "settled record pulls trust under the floor",
                rogue_trust < TRUST_FLOOR,
                f"{rogue_trust} < {TRUST_FLOOR}",
            )
            engine.close()

    # -- 5: enforce mode ------------------------------------------------------
    with ClaimStore(DB_PATH) as store:
        n_before = len(store.claims())

    with gateway_session(policy="enforce", min_trust=TRUST_FLOOR) as gw:
        blocked, message = False, ""
        try:
            gw.call(
                "update_description",
                {
                    "entity_urn": target.urn,
                    "column_path": column,
                    "description": "Another rogue overwrite attempt.",
                    "operation": "replace",
                },
            )
        except RuntimeError as exc:
            blocked, message = True, str(exc)
        check(
            "enforce blocks the low-trust write",
            blocked and "heimdall policy" in message,
            message[:100],
        )

        text = gw.call("search", {"query": "lineworld", "num_results": 5})
        check("reads still allowed under enforce", bool(text))

    with ClaimStore(DB_PATH) as store:
        check(
            "blocked write recorded no claim",
            len(store.claims()) == n_before,
        )

    with DataHubMCP() as plain:
        snap = json.dumps(plain.get_entities([target.urn]))
        check(
            "blocked write never reached the catalog",
            "Another rogue overwrite" not in snap and original[:35] in snap,
        )

    print(f"\n{len(failures)} failures" if failures else "\nALL CHECKS PASSED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
