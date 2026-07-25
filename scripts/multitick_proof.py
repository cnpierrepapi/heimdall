"""T7 live proof: several unattended ticks accumulate into a sharper leaderboard.

Runs the scheduled entrypoint several times end to end against real DataHub and a
real LLM, exactly as the timer will: generate, ingest, cast, ground, settle,
publish. Then asserts what the schedule is actually for, which one tick cannot
show: each tick builds a distinct catalog, the durable stores carry evidence
forward, and a recurring agent's settled count climbs so its trust is recomputed
over a deeper record every time.

Everything it publishes is read back through the anonymous console path and then
removed, and every catalog it created is hard-deleted, so the live console is
restored to its pre-launch state.

Two safety notes. The proof runs in a scratch engine home with the cutover
sentinel pre-marked, so it can never retire the real showcase feed. And it
overrides the activation date locally so the ticks actually run while the
installed timer stays dormant until August 1.

Run on the box:
    set -a; . ~/.heimdall/env; set +a
    ~/fresh-e2e/v/bin/python scripts/multitick_proof.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date

import httpx

TICKS = int(os.environ.get("PROOF_TICKS", "3"))
URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
ANON = os.environ.get("SUPABASE_ANON_KEY", "")

REQUIRED = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
            "OPENROUTER_API_KEY", "MCP_SERVER_DATAHUB")

checks: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    checks.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return passed


def anon_rows(table: str, catalog: str) -> list:
    r = httpx.get(
        f"{URL}/rest/v1/{table}",
        params={"catalog": f"eq.{catalog}", "select": "*"},
        headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        print(f"FATAL: missing {', '.join(missing)}", file=sys.stderr)
        return 2

    # run the ticks now rather than waiting for the real activation date
    os.environ["HEIMDALL_START_DATE"] = date.today().isoformat()

    from heimdall.catalog import load_spec
    from heimdall.claims import ClaimStore
    from heimdall.engine import EngineConfig
    from heimdall.ingest import hard_delete_catalog
    from heimdall.llm import DEFAULT_MODEL
    from heimdall.publisher import Publisher
    from heimdall.scheduler import STATUS_OK, mark_cutover, run_once
    from heimdall.trust import trust_report

    home = tempfile.mkdtemp(prefix="heimdall-t7-")
    # never retire the live showcase feed from a proof run
    mark_cutover(home)

    cfg = EngineConfig(
        home=home,
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        mcp_server=os.environ["MCP_SERVER_DATAHUB"],
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
    )

    catalogs: list[str] = []
    history: list[dict] = []
    try:
        for i in range(TICKS):
            print(f"\n== tick {i + 1}/{TICKS} ==")
            run = run_once(cfg, seed=None)
            print(f"  {run.line}")
            if run.status != STATUS_OK:
                continue
            spec_catalog = run.line.split("catalog=")[1].split(" ")[0]
            catalogs.append(spec_catalog)
            report = trust_report(ClaimStore(cfg.trust_db))
            history.append({a: {k: (v["n_settled"], v["trust"], v["verdict"])
                                for k, v in kinds.items()}
                            for a, kinds in report.items()})
            check(f"tick {i + 1} did not retire the showcase", run.cutover is False)

        print("\n== assertions ==")
        ok_ticks = len(catalogs)
        check("at least two ticks ran", ok_ticks >= 2, f"{ok_ticks}/{TICKS} ok")
        check("every tick built a distinct catalog",
              len(set(catalogs)) == ok_ticks, ", ".join(catalogs))

        # a recurring agent is the whole point: same identity, deeper record
        recurring = []
        if len(history) >= 2:
            first, last = history[0], history[-1]
            for agent, kinds in last.items():
                for kind, (n, trust, verdict) in kinds.items():
                    before = first.get(agent, {}).get(kind)
                    if before and n > before[0]:
                        recurring.append((agent, kind, before, (n, trust, verdict)))
        check("a recurring agent's record grew across ticks", bool(recurring),
              f"{len(recurring)} agent/kind pairs deepened")
        for agent, kind, before, after in recurring:
            print(f"    {agent:12} {kind:11} n {before[0]}->{after[0]} "
                  f"trust {before[1]}->{after[1]} verdict {before[2]!r}->{after[2]!r}")

        # the console's own read path sees every tick
        for cat in catalogs:
            act = anon_rows("hd_activity", cat)
            check(f"anon console reads activity for {cat}", len(act) > 0, f"{len(act)} rows")
        board = httpx.get(
            f"{URL}/rest/v1/hd_agents",
            params={"catalog": f"in.({','.join(catalogs)})", "select": "*"},
            headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"}, timeout=30,
        ).json() if catalogs else []
        check("leaderboard rows published", len(board) > 0,
              f"{len(board)} rows over {len({r['work_kind'] for r in board})} work kinds")

    finally:
        print("\n== cleanup ==")
        try:
            with Publisher() as pub:
                for table in ("hd_activity", "hd_findings", "hd_agents"):
                    pub.delete_catalogs(table, catalogs)
            left = sum(len(anon_rows(t, c)) for t in ("hd_activity", "hd_findings")
                       for c in catalogs)
            check("published proof rows removed", left == 0, f"{left} left")
        except Exception as exc:
            check("published proof rows removed", False, str(exc)[:120])

        deleted = 0
        for cat in catalogs:
            spec_path = os.path.join(cfg.spec_dir, f"{cat}.json")
            if os.path.exists(spec_path):
                try:
                    hard_delete_catalog(load_spec(spec_path), gms_url=cfg.gms_url)
                    deleted += 1
                except Exception:
                    pass
        check("proof catalogs hard-deleted from DataHub", deleted == len(catalogs),
              f"{deleted}/{len(catalogs)}")
        shutil.rmtree(home, ignore_errors=True)

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
