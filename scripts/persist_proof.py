"""W1 live proof: the world survives the tick that works it.

The churn engine deleted its catalog every tick, which made any settlement that
needs time impossible: a claim about a dataset cannot be checked tomorrow if the
dataset is gone. This proves the fix against real DataHub rather than against a
stub, because the failure mode it rules out is precisely one that unit tests with
a fake emitter cannot see: an entity that exists in the store but was never
actually written, or was written twice under two identities.

Two consecutive ticks on a single world, then the assertions that matter:

  * both ticks worked the SAME catalog, and its clock moved 1 -> 2,
  * the dataset URN set is byte-identical across the ticks, so console deep links
    published on day one still point at something on day two,
  * every one of those URNs actually resolves in DataHub after both ticks,
  * DataHub was written once, not once per tick (no duplicate ingest), and
  * nothing landed in the retention directory, so garbage collection structurally
    cannot reach a persistent world.

Runs in a scratch engine home with its own worlds, so the real showcase feed and
its accumulated trust are untouched. Overrides the activation date locally while
the installed timer stays dormant. Hard-deletes the world it created on the way
out, since a proof world is not a showcase world.

Run on the box:
    set -a; . ~/.heimdall/env; set +a
    ~/fresh-e2e/v/bin/python scripts/persist_proof.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date

CAST = int(os.environ.get("PROOF_CAST", "2"))

checks: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    checks.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return passed


def main() -> int:
    for key in ("OPENROUTER_API_KEY", "MCP_SERVER_DATAHUB"):
        if not os.environ.get(key):
            print(f"FATAL: {key} required", file=sys.stderr)
            return 2

    # run now rather than waiting for the real activation date
    os.environ["HEIMDALL_START_DATE"] = date.today().isoformat()

    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

    from heimdall.catalog import spec_to_world
    from heimdall.engine import EngineConfig, run_tick
    from heimdall.ingest import hard_delete_catalog
    from heimdall.llm import DEFAULT_MODEL
    from heimdall.worldstore import WorldStore

    home = tempfile.mkdtemp(prefix="heimdall-w1-")
    cfg = EngineConfig(
        home=home,
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        mcp_server=os.environ["MCP_SERVER_DATAHUB"],
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        cast_size=CAST,
        worlds=1,  # one world, so consecutive ticks must land on it
    )
    graph = DataHubGraph(DatahubClientConfig(server=cfg.gms_url))

    def live_urns(world_id: str) -> tuple[list[str], list[str]]:
        """(all urns of this world, the subset DataHub actually resolves)."""
        with WorldStore(cfg.worlds_db, cfg.worlds_dir) as store:
            world = spec_to_world(store.spec(world_id))
        urns = sorted(d.urn for d in world.datasets.values())
        return urns, [u for u in urns if graph.exists(u)]

    spec = None
    try:
        runs = []
        for i in (1, 2):
            print(f"\n== tick {i}/2 ==")
            res = run_tick(cfg)
            print(f"  ok={res.ok} reason={res.reason} catalog={res.catalog} "
                  f"day={res.day} ingested={res.ingested} seed={res.seed}")
            for s in res.stats:
                print(f"    {s.agent_id:12} {s.work_kind:11} {s.profile:8} "
                      f"proposed={s.proposed} applied={s.applied} blocked={s.blocked}")
            if res.ok:
                print(f"    events={res.n_events} findings={res.n_findings} "
                      f"settle={res.settle} spend=${res.spend_tick:.4f}")
                all_urns, resolved = live_urns(res.catalog)
                print(f"    urns={len(all_urns)} resolved in DataHub={len(resolved)}")
                runs.append((res, all_urns, resolved))
            else:
                runs.append((res, [], []))

        print("\n== assertions ==")
        both_ok = all(r.ok for r, _, _ in runs)
        check("both ticks ran", both_ok,
              " / ".join(f"tick {i + 1}: {r.reason}" for i, (r, _, _) in enumerate(runs)))
        if not both_ok:
            raise SystemExit(1)

        (r1, urns1, live1), (r2, urns2, live2) = runs

        check("both ticks worked the same world", r1.catalog == r2.catalog,
              f"{r1.catalog} then {r2.catalog}")
        check("the world's clock advanced one day per tick",
              (r1.day, r2.day) == (1, 2), f"day {r1.day} then {r2.day}")
        check("the urn set is byte-identical across ticks", urns1 == urns2,
              f"{len(urns1)} urns, {len(set(urns1) ^ set(urns2))} differing")
        check("every urn resolves in DataHub after tick 1", live1 == urns1,
              f"{len(live1)}/{len(urns1)}")
        check("every urn still resolves after tick 2", live2 == urns2,
              f"{len(live2)}/{len(urns2)}")
        check("DataHub was written once, not once per tick",
              r1.ingested and not r2.ingested,
              f"ingested tick1={r1.ingested} tick2={r2.ingested}")
        check("agents did real work on the persistent world",
              any(s.applied > 0 for r, _, _ in runs for s in r.stats),
              f"{sum(s.applied for r, _, _ in runs for s in r.stats)} writes applied")
        check("retention cannot see the world",
              os.listdir(cfg.spec_dir) == [] and not r1.gc_deleted and not r2.gc_deleted,
              f"spec_dir={os.listdir(cfg.spec_dir)}")

        with WorldStore(cfg.worlds_db, cfg.worlds_dir) as store:
            spec = store.spec(r1.catalog)
            check("the world spec is still on disk under its own id",
                  spec.catalog == r1.catalog)

    finally:
        print("\n== cleanup ==")
        if spec is not None:
            gone = sum(1 for r in hard_delete_catalog(spec, gms_url=cfg.gms_url) if r.ok)
            check("proof world hard-deleted from DataHub", gone == len(spec.datasets),
                  f"{gone}/{len(spec.datasets)} datasets")
        shutil.rmtree(home, ignore_errors=True)

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
