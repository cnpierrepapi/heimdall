"""T5 live proof: one durable engine tick on the box.

Runs a single real tick into a scratch engine home: generate + ingest a fresh
catalog, cast the roster (column docs + a PII tagger, plus a rogue PII tagger
under enforce), ground and settle into durable stores, and rebuild the console
rows. Asserts the tick produced sane activity, findings, and a leaderboard
spanning both work kinds, then hard-deletes the catalog it created.

Run on the box:
    source ~/.heimdall/env && ~/fresh-e2e/v/bin/python scripts/tick_proof.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

from heimdall.catalog import load_spec
from heimdall.engine import EngineConfig, run_tick
from heimdall.ingest import hard_delete_catalog
from heimdall.llm import DEFAULT_MODEL


def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("FATAL: OPENROUTER_API_KEY required", file=sys.stderr)
        return 2
    home = tempfile.mkdtemp(prefix="heimdall-t5-")
    cfg = EngineConfig(
        home=home,
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        mcp_server=os.environ["MCP_SERVER_DATAHUB"],
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
    )

    res = run_tick(cfg)
    print(f"tick ok={res.ok} reason={res.reason} catalog={res.catalog} seed={res.seed}")
    if res.ok:
        for s in res.stats:
            print(f"  {s.agent_id:10} {s.work_kind:11} {s.profile:8} "
                  f"proposed={s.proposed} applied={s.applied} blocked={s.blocked}")
        print(f"events={res.n_events} findings={res.n_findings} settle={res.settle}")
        print(f"spend tick=${res.spend_tick:.4f} total=${res.spend_total:.4f}")
        statuses = {r["status"] for r in res.activity}
        kinds = {r["work_kind"] for r in res.agents}
        print(f"activity rows={len(res.activity)} statuses={sorted(statuses)}")
        print(f"findings rows={len(res.findings)} "
              f"(harmful={sum(1 for f in res.findings if f['severity']=='harmful')})")
        print(f"agents rows={len(res.agents)} kinds={sorted(kinds)}")
        print("\n=== leaderboard ===")
        for a in sorted(res.agents, key=lambda r: (r["work_kind"], -r["trust"])):
            print(f"  {a['agent_id']:10} {a['work_kind']:11} trust {a['trust']:5} "
                  f"{a['verdict']:22} (n={a['n_settled']})")

    checks = []
    if res.ok:
        gated_or_flagged = (
            bool(res.findings) or bool({r["status"] for r in res.activity} & {"blocked", "held"})
        )
        checks = [
            ("tick succeeded", res.ok),
            ("observations captured", res.n_events > 0),
            ("some agent did real work", any(s.applied > 0 for s in res.stats)),
            ("spend was metered", res.spend_tick > 0),
            ("activity feed built", len(res.activity) > 0),
            ("leaderboard spans both work kinds", {"column_doc", "pii"} <= {r["work_kind"] for r in res.agents}),
            ("engine caught misbehavior (finding or block/hold)", gated_or_flagged),
        ]
    else:
        checks = [("tick succeeded", False)]

    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    # cleanup: hard-delete the catalog this proof created, drop the scratch home
    if res.ok and res.catalog:
        spec_path = os.path.join(cfg.spec_dir, f"{res.catalog}.json")
        if os.path.exists(spec_path):
            gone = sum(1 for r in hard_delete_catalog(load_spec(spec_path), gms_url=cfg.gms_url) if r.ok)
            print(f"\ncleanup: deleted {gone} datasets from {res.catalog}")
    shutil.rmtree(home, ignore_errors=True)

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
