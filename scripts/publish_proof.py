"""T6 live proof: run a tick, publish to Supabase, verify via the anon path.

Runs one real engine tick, publishes its rows with the service key, then reads
them back through the anonymous PostgREST path the console uses (proving the rows
are visible to the public console under RLS). It cleans up everything it wrote
and hard-deletes the catalog it created, so the live console is not polluted
before the engine's real activation.

Run on the box:
    source ~/.heimdall/env && ~/fresh-e2e/v/bin/python scripts/publish_proof.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import httpx

from heimdall.catalog import load_spec
from heimdall.engine import EngineConfig, run_tick
from heimdall.ingest import hard_delete_catalog
from heimdall.llm import DEFAULT_MODEL
from heimdall.publisher import Publisher

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
ANON = os.environ.get("SUPABASE_ANON_KEY", "")


def anon_get(table: str, catalog: str) -> list:
    r = httpx.get(
        f"{URL}/rest/v1/{table}",
        params={"catalog": f"eq.{catalog}", "select": "*"},
        headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
                 "OPENROUTER_API_KEY", "MCP_SERVER_DATAHUB"):
        if not os.environ.get(name):
            print(f"FATAL: env {name} required", file=sys.stderr)
            return 2

    home = tempfile.mkdtemp(prefix="heimdall-t6-")
    cfg = EngineConfig(
        home=home,
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        mcp_server=os.environ["MCP_SERVER_DATAHUB"],
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
    )

    res = run_tick(cfg)
    print(f"tick ok={res.ok} catalog={res.catalog} "
          f"activity={len(res.activity)} findings={len(res.findings)} agents={len(res.agents)}")
    if not res.ok:
        print(f"tick did not run: {res.reason}")
        shutil.rmtree(home, ignore_errors=True)
        return 1

    with Publisher() as pub:
        counts = pub.publish_tick(res)
    print(f"published: {counts}")

    # read back through the anonymous path the console uses
    a = anon_get("hd_activity", res.catalog)
    f = anon_get("hd_findings", res.catalog)
    g = anon_get("hd_agents", res.catalog)
    print(f"anon read: activity={len(a)} findings={len(f)} agents={len(g)}")

    checks = [
        ("activity published and anon-visible", len(a) == len(res.activity) and len(a) > 0),
        ("findings published and anon-visible", len(f) == len(res.findings)),
        ("leaderboard published and anon-visible", len(g) == len(res.agents) and len(g) > 0),
        ("leaderboard spans both work kinds", {r["work_kind"] for r in g} >= {"column_doc", "pii"}),
        ("feed carries a blocked or ok status", bool({r["status"] for r in a} & {"blocked", "ok"})),
    ]
    print("\n=== checks ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    # cleanup: remove everything this proof wrote, and the catalog it created
    print("\ncleanup: deleting this proof's rows and catalog")
    with Publisher() as pub:
        pub.delete_catalogs("hd_activity", [res.catalog])
        pub.delete_catalogs("hd_findings", [res.catalog])
        # agents rows carry the proof catalog id, so scope the delete by it
        pub._client.delete(
            f"{pub.url}/rest/v1/hd_agents?catalog=eq.{res.catalog}",
            headers=pub._headers("return=minimal"),
        )
    left = len(anon_get("hd_activity", res.catalog)) + len(anon_get("hd_agents", res.catalog))
    print(f"rows remaining for {res.catalog} after cleanup: {left}")
    spec_path = os.path.join(cfg.spec_dir, f"{res.catalog}.json")
    if os.path.exists(spec_path):
        gone = sum(1 for r in hard_delete_catalog(load_spec(spec_path), gms_url=cfg.gms_url) if r.ok)
        print(f"deleted {gone} datasets from DataHub")
    shutil.rmtree(home, ignore_errors=True)

    ok = ok and left == 0
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
