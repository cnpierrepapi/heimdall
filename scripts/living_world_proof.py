"""The full living-world proof: many days, one world, real DataHub, real agents.

This is the proof the whole persistent-world design stands or falls on, and the
one that could not be written before the world could change. It runs a run of
consecutive days on a single world against live DataHub and a live model, and
then asks the questions that only a multi-day run can answer.

What it holds the system to:

  * ONE WORLD, MANY DAYS. Every tick works the same catalog and moves its clock
    on exactly one day. Nothing is regenerated, nothing is deleted.

  * THE CATALOG REALLY CHANGES, AND DATAHUB AGREES. Each day after the first
    mutates the world, and every mutation is verified against live DataHub, not
    against our own spec: an added column is present in the real schema, a dropped
    column is really gone, a rotted column really reads as undocumented again, an
    added table really resolves as an entity.

  * NOTHING THAT EXISTED EVER MOVES. Every dataset urn seen on day one still
    resolves on the last day. A settled claim and a published console deep link
    both point at a urn, so a design that quietly re-mints them is worthless.

  * AGENTS WORK THE NEW ARTIFACTS AND LEAVE THE FINISHED ONES ALONE. Rewrites
    stay at zero across the whole run: not because agents are told to behave, but
    because a documented column is not offered as work.

  * THE ACCEPT RATE DOES NOT DECAY WITH WORLD AGE. This is the finding that
    started all of it. Before evolution, day two collapsed from 7 accepts to 2
    while reverts tripled, purely because the world had been used up. Trust that
    falls because a catalog got older is not measuring the agent.

  * SCORING STAYS HONEST. No agent banks the same column twice, and the unscoreable
    kinds settle nothing at all.

Runs in a scratch engine home so the live showcase feed is untouched, overrides
the activation date locally while the installed timer stays dormant, and hard
deletes the world it created on the way out.

Run on the box:
    set -a; . ~/.heimdall/env; set +a
    PROOF_DAYS=5 ~/fresh-e2e/v/bin/python scripts/living_world_proof.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date

DAYS = int(os.environ.get("PROOF_DAYS", "5"))
CAST = int(os.environ.get("PROOF_CAST", "3"))
MUTATIONS = int(os.environ.get("PROOF_MUTATIONS", "2"))

checks: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    checks.append((name, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return bool(passed)


def main() -> int:
    for key in ("OPENROUTER_API_KEY", "MCP_SERVER_DATAHUB"):
        if not os.environ.get(key):
            print(f"FATAL: {key} required", file=sys.stderr)
            return 2

    # run the days now rather than waiting for the real activation date
    os.environ["HEIMDALL_START_DATE"] = date.today().isoformat()

    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

    from heimdall.catalog import spec_to_world
    from heimdall.claims import ClaimStore
    from heimdall.engine import EngineConfig, run_tick
    from heimdall.evolve import ADD_COLUMN, ADD_PII_COLUMN, ADD_TABLE, DOC_ROT, DROP_COLUMN
    from heimdall.ingest import hard_delete_catalog
    from heimdall.llm import DEFAULT_MODEL
    from heimdall.mcp_client import DataHubMCP
    from heimdall.trust import trust_report
    from heimdall.worldstore import WorldStore

    home = tempfile.mkdtemp(prefix="heimdall-lw-")
    cfg = EngineConfig(
        home=home,
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        mcp_server=os.environ["MCP_SERVER_DATAHUB"],
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        cast_size=CAST,
        worlds=1,  # one world, so every day lands on it
        mutations_per_day=MUTATIONS,
    )
    graph = DataHubGraph(DatahubClientConfig(server=cfg.gms_url))

    def live_schema(urn: str) -> dict:
        """The real column state in DataHub, keyed by column name."""
        with DataHubMCP(gms_url=cfg.gms_url) as mcp:
            fields = mcp.list_schema_fields(urn).get("fields", [])
        return {f["fieldPath"]: f for f in fields if f.get("fieldPath")}

    def world_urns(world_id: str) -> dict[str, str]:
        with WorldStore(cfg.worlds_db, cfg.worlds_dir) as store:
            world = spec_to_world(store.spec(world_id))
        return {name: ds.urn for name, ds in world.datasets.items()}

    spec = None
    days: list = []
    urns_by_day: list[dict[str, str]] = []
    try:
        for i in range(1, DAYS + 1):
            print(f"\n== day {i}/{DAYS} ==")
            res = run_tick(cfg)
            if not res.ok:
                check(f"day {i} ran", False, res.reason)
                break
            urns_by_day.append(world_urns(res.catalog))
            print(f"  catalog={res.catalog} day={res.day} ingested={res.ingested}")
            for m in res.mutations:
                print(f"    changed: {m['kind']:15} {m.get('dataset')}"
                      f"{'.' + m['column'] if m.get('column') else ''}")
            for s in res.stats:
                print(f"    {s.agent_id:12} {s.work_kind:11} {s.profile:8} "
                      f"proposed={s.proposed} applied={s.applied} blocked={s.blocked}")
            print(f"    events={res.n_events} findings={res.n_findings} "
                  f"settle={res.settle} spend=${res.spend_tick:.4f}")
            days.append(res)

        print("\n== assertions ==")
        ran = len(days)
        if not check(f"all {DAYS} days ran", ran == DAYS, f"{ran}/{DAYS}"):
            raise SystemExit(1)

        world_id = days[0].catalog
        check("every day worked the same world",
              all(d.catalog == world_id for d in days), world_id)
        check("the clock advanced one day per tick",
              [d.day for d in days] == list(range(1, DAYS + 1)),
              f"days {[d.day for d in days]}")
        check("DataHub was written once, not once per day",
              [d.ingested for d in days] == [True] + [False] * (DAYS - 1),
              f"ingested {[d.ingested for d in days]}")

        # -- the world really changed, checked against DataHub itself ----------
        all_mutations = [m for d in days for m in d.mutations]
        kinds = Counter(m["kind"] for m in all_mutations)
        check("the catalog changed on every day after the first",
              all(d.mutations for d in days[1:]) and not days[0].mutations,
              f"{len(all_mutations)} changes: {dict(kinds)}")
        check("the changes were of more than one kind", len(kinds) > 1,
              ", ".join(sorted(kinds)))

        urns = urns_by_day[-1]
        worked_columns = _worked_columns(cfg)
        verified = Counter()
        failed: list[str] = []
        for m in all_mutations:
            ds, col = m.get("dataset"), m.get("column")
            if m["kind"] == ADD_TABLE:
                ok = ds in urns and graph.exists(urns[ds])
            elif ds not in urns:
                ok = False
            else:
                live = live_schema(urns[ds])
                if m["kind"] in (ADD_COLUMN, ADD_PII_COLUMN):
                    ok = col in live
                elif m["kind"] == DROP_COLUMN:
                    ok = col not in live
                elif m["kind"] == DOC_ROT:
                    # rot is proven by the column being open work again, which is
                    # true if it still reads blank OR an agent has since written a
                    # description to it. reading blank at the end of the run is not
                    # the test: an agent doing the freed work is the point of it.
                    f = live.get(col, {})
                    blank = not (str(f.get("description") or "").strip()
                                 or str(f.get("editedDescription") or "").strip())
                    ok = col in live and (blank or (urns[ds], col) in worked_columns)
                else:
                    ok = False
            verified[m["kind"]] += int(ok)
            if not ok:
                failed.append(f"{m['kind']} {ds}.{col}")
        check("every change is real in DataHub, not just in our spec",
              not failed, f"verified {sum(verified.values())}/{len(all_mutations)}"
              + (f"; unverified: {'; '.join(failed[:4])}" if failed else ""))

        # -- nothing that existed ever moved -----------------------------------
        day_one, final = urns_by_day[0], urns_by_day[-1]
        kept = {n: u for n, u in day_one.items() if final.get(n) == u}
        check("every dataset urn from day one is unchanged on the last day",
              len(kept) == len(day_one),
              f"{len(kept)}/{len(day_one)} unchanged")
        resolved = {n: graph.exists(u) for n, u in day_one.items()}
        check("every day-one dataset still resolves in DataHub after every change",
              all(resolved.values()), f"{sum(resolved.values())}/{len(resolved)}")
        check("the world grew rather than being replaced",
              len(final) >= len(day_one),
              f"{len(day_one)} datasets on day one, {len(final)} at the end")

        # -- agents worked new artifacts and left finished ones alone ----------
        settle = Counter()
        for d in days:
            settle.update(d.settle)
        check("agents did real work every day after the first",
              all(any(s.applied for s in d.stats) for d in days[1:]),
              f"{sum(s.applied for d in days for s in d.stats)} writes applied")
        check("no agent was scored for redoing finished work",
              settle["rewrite"] == 0, f"rewrite={settle['rewrite']}")

        # THE finding this whole phase exists to answer
        rates = [(d.settle.get("accepted", 0), d.settle.get("settled", 0)) for d in days]
        worked = [(a, s) for a, s in rates if s]
        print("\n  per-day accept rate: " + "  ".join(
            f"day{i + 1} {a}/{s}" for i, (a, s) in enumerate(rates)))
        check("later days still settle work",
              len(worked) >= max(2, DAYS - 1),
              f"{len(worked)}/{DAYS} days settled anything")
        first_rate = worked[0][0] / worked[0][1]
        last_rate = worked[-1][0] / worked[-1][1]
        check("the accept rate did not decay with world age",
              last_rate >= first_rate * 0.5,
              f"first {first_rate:.0%} -> last {last_rate:.0%}")

        # -- scoring stayed honest --------------------------------------------
        store = ClaimStore(cfg.trust_db)
        report = trust_report(store)
        print("\n  === leaderboard after "
              f"{DAYS} simulated days ===")
        for agent, by_kind in sorted(report.items()):
            for kind, rec in sorted(by_kind.items()):
                print(f"    {agent:12} {kind:11} trust {str(rec['trust']):>5} "
                      f"n={rec['n_settled']:<3} {rec['score_state']:12} {rec['verdict']}")
        seen = Counter()
        for c in store.claims():
            if c.settled:
                seen[(c.agent_id, c.entity_urn, c.prediction.get("column"))] += 1
        check("no agent was scored twice on the same column",
              not any(v > 1 for v in seen.values()),
              f"{len(seen)} distinct scored surfaces")
        unscoreable = [rec for by_kind in report.values() for kind, rec in by_kind.items()
                       if rec["score_state"] == "unscoreable"]
        check("unscoreable work settled nothing",
              all(r["n_settled"] == 0 for r in unscoreable),
              f"{len(unscoreable)} unscoreable agent/kind rows")

        with WorldStore(cfg.worlds_db, cfg.worlds_dir) as store2:
            logged = store2.mutations(world_id=world_id)
            spec = store2.spec(world_id)
        check("the mutation log matches what the ticks reported",
              len(logged) == len(all_mutations)
              and [m.kind for m in logged] == [m["kind"] for m in all_mutations],
              f"{len(logged)} logged")
        # what a forecasting agent is allowed to read: the days already lived,
        # never the one being predicted
        history = store_mutations(cfg, world_id, DAYS)
        check("history stops short of the day being predicted",
              all(m.day < DAYS for m in history) and len(history) < len(logged),
              f"{len(history)} of {len(logged)} changes visible before day {DAYS}")
        check("retention never saw the living world",
              os.listdir(cfg.spec_dir) == [] and not any(d.gc_deleted for d in days))

        print(f"\n  total spend ${days[-1].spend_total:.4f} over {DAYS} days")

    finally:
        print("\n== cleanup ==")
        if spec is None:
            try:
                with WorldStore(cfg.worlds_db, cfg.worlds_dir) as store3:
                    rec = store3.next_world()
                    spec = store3.spec(rec.world_id) if rec else None
            except Exception:
                spec = None
        if spec is not None:
            gone = sum(1 for r in hard_delete_catalog(spec, gms_url=cfg.gms_url) if r.ok)
            check("proof world hard-deleted from DataHub", gone == len(spec.datasets),
                  f"{gone}/{len(spec.datasets)} datasets")
        shutil.rmtree(home, ignore_errors=True)

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


def store_mutations(cfg, world_id: str, before_day: int):
    from heimdall.worldstore import WorldStore
    with WorldStore(cfg.worlds_db, cfg.worlds_dir) as store:
        return store.mutations(world_id=world_id, before_day=before_day)


def _worked_columns(cfg) -> set[tuple[str, str]]:
    """(dataset urn, column) pairs some agent has recorded a claim about."""
    from heimdall.claims import ClaimStore
    store = ClaimStore(cfg.trust_db)
    return {(c.entity_urn, c.prediction.get("column")) for c in store.claims()}


if __name__ == "__main__":
    sys.exit(main())
