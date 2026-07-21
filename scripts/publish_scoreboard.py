"""Publish the ledger to the public scoreboard (Supabase).

Reads the ledger SQLite database, recomputes the skill report, and upserts
three projections through PostgREST: per-agent records, calibration bins,
and the full claim history. Idempotent: re-running replaces prior rows for
the same agents and claims.

Env:
  LEDGER_DB              path to the ledger database
  SUPABASE_URL           https://<project>.supabase.co
  SUPABASE_SERVICE_KEY   service-role key (writes bypass RLS; never ship it
                         to the browser)

Run:  python scripts/publish_scoreboard.py
"""

from __future__ import annotations

import os
import sys

import httpx

from heimdall.claims import ClaimStore
from heimdall.publish import build_rows

DB_PATH = os.environ.get("LEDGER_DB", os.path.expanduser("~/heimdall-demo.db"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

CONFLICT_KEYS = {
    "ll_agents": "agent_id",
    "ll_calibration": "agent_id,bin_low",
    "ll_claims": "claim_id",
}


def upsert(client: httpx.Client, table: str, rows: list[dict], conflict: str) -> None:
    if not rows:
        return
    resp = client.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}",
        json=rows,
        headers={
            "apikey": SERVICE_KEY,
            "Authorization": f"Bearer {SERVICE_KEY}",
            "Prefer": "resolution=merge-duplicates",
            "Content-Type": "application/json",
        },
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"{table} upsert failed: {resp.status_code} {resp.text[:400]}")
    print(f"  {table}: {len(rows)} rows")


def main() -> None:
    if not SUPABASE_URL or not SERVICE_KEY:
        sys.exit("set SUPABASE_URL and SUPABASE_SERVICE_KEY")

    with ClaimStore(DB_PATH) as store:
        tables = build_rows(store)

    print(f"publishing to {SUPABASE_URL}")
    with httpx.Client(timeout=30) as client:
        for table in ("ll_agents", "ll_calibration", "ll_claims"):
            upsert(client, table, tables[table], CONFLICT_KEYS[table])
    print("done")


if __name__ == "__main__":
    main()
