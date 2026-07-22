"""Publish a tick's rows to Supabase over PostgREST with the service key.

The engine produces plain row dicts; this writes them to the three public tables
the console reads. Activity and findings are append-only inserts; the leaderboard
is an upsert on (agent_id, work_kind) so an agent's trust row is updated in place
as its record grows. Retention runs in lockstep with the catalog GC: when a tick
hard-deletes old catalogs from DataHub, their activity and findings rows are
deleted here too, so the console's DataHub deep-links never point at a catalog
that no longer exists. The leaderboard is not GC'd; it is bounded by roster size
times work kinds and is the accumulated record.

The service key is a secret read from the environment; it is never logged.
"""

from __future__ import annotations

import os
from typing import Any, Optional

SHOWCASE = "showcase"


class Publisher:
    def __init__(self, url: Optional[str] = None, service_key: Optional[str] = None,
                 client: Any = None, timeout: float = 30.0):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.url or not self.key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        if client is None:
            import httpx
            client = httpx.Client(timeout=timeout)
        self._client = client

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Publisher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _headers(self, prefer: Optional[str] = None) -> dict[str, str]:
        h = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    def _check(self, resp: Any, what: str) -> None:
        if resp.status_code >= 300:
            raise RuntimeError(f"{what} failed: HTTP {resp.status_code} {resp.text[:300]}")

    def insert(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        resp = self._client.post(
            f"{self.url}/rest/v1/{table}", json=rows,
            headers=self._headers("return=minimal"),
        )
        self._check(resp, f"insert {table}")
        return len(rows)

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> int:
        if not rows:
            return 0
        resp = self._client.post(
            f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}", json=rows,
            headers=self._headers("resolution=merge-duplicates,return=minimal"),
        )
        self._check(resp, f"upsert {table}")
        return len(rows)

    def delete_catalogs(self, table: str, catalogs: list[str], owner: str = SHOWCASE) -> None:
        if not catalogs:
            return
        inlist = ",".join(catalogs)
        resp = self._client.delete(
            f"{self.url}/rest/v1/{table}?owner=eq.{owner}&catalog=in.({inlist})",
            headers=self._headers("return=minimal"),
        )
        self._check(resp, f"delete {table}")

    def publish_tick(self, result: Any) -> dict[str, int]:
        """Append this tick's activity + findings, upsert agents, GC retired rows."""
        counts = {
            "activity": self.insert("hd_activity", result.activity),
            "findings": self.insert("hd_findings", result.findings),
            "agents": self.upsert("hd_agents", result.agents, on_conflict="agent_id,work_kind"),
        }
        for table in ("hd_activity", "hd_findings"):
            self.delete_catalogs(table, result.gc_deleted)
        return counts

    def reset_showcase(self) -> None:
        """Clear the showcase feed. Used once when cutting over to the engine.

        Not called automatically: retiring the existing curated feed before the
        engine is producing would leave the console blank, so this is a manual
        cutover step.
        """
        for table in ("hd_activity", "hd_findings"):
            resp = self._client.delete(
                f"{self.url}/rest/v1/{table}?owner=eq.{SHOWCASE}",
                headers=self._headers("return=minimal"),
            )
            self._check(resp, f"reset {table}")
