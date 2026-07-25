"""Cast the owner agents against a real catalog and publish what they did.

Ownership cannot be scored here and never will be: a catalog does not know who
owns it, so the owner was assigned rather than derived and a correct guess is
luck. Heimdall says so in place of a score. This proves the rest of the claim,
which is the part that matters: the agents still run, every action is observed,
and a proposal that contradicts the catalog is still caught and cited.

Unlike the other proofs this one KEEPS what it publishes. The point is a real
unscoreable agent on the live console, produced by real work on real DataHub, so
the catalog it built is left in place and its deep links keep resolving.

Run on the box:
    set -a; . ~/.heimdall/env; set +a
    ~/fresh-e2e/v/bin/python scripts/owner_cast_proof.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

import httpx

REQUIRED = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
            "OPENROUTER_API_KEY", "MCP_SERVER_DATAHUB")

checks: list[tuple[str, bool]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    checks.append((name, passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return passed


def main() -> int:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        print(f"FATAL: missing {', '.join(missing)}", file=sys.stderr)
        return 2
    os.environ["HEIMDALL_START_DATE"] = date.today().isoformat()

    from heimdall.budget import SpendLedger
    from heimdall.catalog import save_spec, spec_to_world
    from heimdall.claims import ClaimStore
    from heimdall.conduct import conduct_by_kind
    from heimdall.engine import EngineConfig, _run_agent, registry
    from heimdall.generator import generate_catalog
    from heimdall.grounding import FindingStore, WorldCatalogContext, ground_events
    from heimdall.ingest import ingest_spec
    from heimdall.llm import DEFAULT_MODEL
    from heimdall.observability import EventStore
    from heimdall.publisher import Publisher
    from heimdall.roster import KIND_OWNER, ROSTER
    from heimdall.snapshot import activity_rows, agents_rows, findings_rows
    from heimdall.trust import settle_observations
    from heimdall.workkinds import UNSCOREABLE

    home = os.path.expanduser("~/.heimdall/owner-proof")
    os.makedirs(os.path.join(home, "catalogs"), exist_ok=True)
    cfg = EngineConfig(
        home=home,
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        mcp_server=os.environ["MCP_SERVER_DATAHUB"],
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
    )

    import time
    start = time.time()
    spec = generate_catalog(int(start))
    save_spec(spec, os.path.join(cfg.spec_dir, f"{spec.catalog}.json"))
    world = spec_to_world(spec)
    teams = tuple(sorted({d.owner for d in spec.datasets if d.owner}))
    urns = [world.datasets[d.name].urn for d in spec.datasets if d.name.startswith("raw_")]
    print(f"catalog {spec.catalog} ({spec.theme}), {len(urns)} raw datasets, teams {teams}")

    ingest_spec(spec, gms_url=cfg.gms_url)

    spend = SpendLedger(cfg.spend_db)
    owners = [a for a in ROSTER if a.work_kind == KIND_OWNER]
    stats = [_run_agent(cfg, a, spend, urns, teams=teams) for a in owners]
    for s in stats:
        print(f"  {s.agent_id:12} {s.profile:9} proposed={s.proposed} "
              f"applied={s.applied} blocked={s.blocked}")
    check("both owner agents ran", len(stats) == 2)
    check("they proposed real ownership", sum(s.proposed for s in stats) > 0,
          f"{sum(s.proposed for s in stats)} proposals")

    events = EventStore(cfg.events_db).events(since_ts=start)
    ctx = WorldCatalogContext(world)
    with FindingStore(cfg.findings_db) as fs:
        ground_events(events, ctx, fs)
        finds = [f for f in fs.findings() if f.ts >= start]
    trust_store = ClaimStore(cfg.trust_db)
    settle = settle_observations(events, ctx, trust_store)

    check("their actions were observed", len(events) > 0, f"{len(events)} events")
    check("nothing settled, so no trust was earned", settle["settled"] == 0,
          f"recorded={settle['recorded']} settled={settle['settled']}")
    wrong = [f for f in finds if f.check_type == "wrong_owner"]
    check("a wrong owner is still caught and cited", len(wrong) > 0,
          f"{len(wrong)} wrong_owner findings")
    if wrong:
        print(f"    e.g. {wrong[0].reason}")

    by_kind = conduct_by_kind(events, finds)
    for (agent, kind), c in sorted(by_kind.items()):
        print(f"    conduct {agent:12} {kind}: actions={c.actions} applied={c.applied} "
              f"harmful={c.harmful} assets={len(c.entities)}")

    with FindingStore(cfg.findings_db) as fs:
        rows = agents_rows(trust_store, registry=registry(), catalog=spec.catalog,
                           event_store=EventStore(cfg.events_db), finding_store=fs)
        finding_rows = findings_rows(fs, catalog=spec.catalog, since_ts=start)
    owner_rows = [r for r in rows if r["work_kind"] == KIND_OWNER]
    check("owner rows are marked unscoreable",
          bool(owner_rows) and all(r["score_state"] == UNSCOREABLE for r in owner_rows),
          f"{len(owner_rows)} rows")
    # unscoreable claims are still recorded, so the skill engine reports the
    # untouched neutral prior rather than nothing at all. What must be true is
    # that no evidence moved it and that conduct is there in its place.
    check("and carry a conduct record instead of an earned score",
          all(r["n_settled"] == 0 and r["trust"] in (None, 50.0)
              and r.get("n_actions", 0) > 0 for r in owner_rows))
    if owner_rows:
        print(f"    reason: {owner_rows[0]['score_reason']}")

    act_rows = activity_rows(EventStore(cfg.events_db), catalog=spec.catalog, since_ts=start)
    with Publisher() as pub:
        counts = {
            "activity": pub.insert("hd_activity", act_rows),
            "findings": pub.insert("hd_findings", finding_rows),
            "agents": pub.upsert("hd_agents", rows, on_conflict="agent_id,work_kind"),
        }
    print(f"published {counts}")

    anon = httpx.get(
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/hd_agents",
        params={"work_kind": "eq.owner", "select": "*"},
        headers={"apikey": os.environ["SUPABASE_ANON_KEY"],
                 "Authorization": f"Bearer {os.environ['SUPABASE_ANON_KEY']}"},
        timeout=30,
    ).json()
    check("the public console can read the unscoreable rows",
          bool(anon) and all(r["score_state"] == UNSCOREABLE for r in anon),
          f"{len(anon)} rows via anon")

    print(f"\ncatalog {spec.catalog} LEFT IN PLACE so the console deep links resolve")
    passed = sum(1 for _, ok in checks if ok)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
