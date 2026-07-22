"""Spend ledger, hard budget cap, and the engine activation gate.

Every LLM call the engine makes is metered into a durable ledger. The engine
runs real agents against a hard ceiling (default $100): when cumulative spend
reaches the cap, the pipeline STOPS - no new catalogs, no agent work, the console
simply holds its last snapshot. There is no scripted fallback; the run is meant to
end honestly when the budget is gone, and the operator can raise the cap to resume.

The same gate enforces an activation date: the engine is a no-op before it, so it
can be installed ahead of time and only begins operating on the start date.

The ledger is the source of truth for spend, not the token math: OpenRouter returns
the real credit cost per call and we store it; the per-token estimate is only a
fallback when a provider omits cost.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_CAP_USD = 100.0
DEFAULT_START_DATE = "2026-08-01"

# fallback per-token prices (USD per token) when a provider omits cost. Kept
# deliberately conservative; the open-weight default is cheap.
_FALLBACK_PRICE = {
    "prompt": 0.20 / 1_000_000,
    "completion": 0.60 / 1_000_000,
}


def budget_cap() -> float:
    return float(os.environ.get("HEIMDALL_LLM_BUDGET_USD", DEFAULT_CAP_USD))


def tick_subcap() -> float:
    """Max spend a single tick may add, so one tick cannot blow the budget."""
    return float(os.environ.get("HEIMDALL_TICK_BUDGET_USD", "2.0"))


def start_date() -> date:
    raw = os.environ.get("HEIMDALL_START_DATE", DEFAULT_START_DATE)
    return datetime.strptime(raw, "%Y-%m-%d").date()


def cost_from_usage(usage: dict, prices: Optional[dict] = None) -> float:
    """Actual credit cost if the provider reported it, else a token estimate."""
    if not usage:
        return 0.0
    cost = usage.get("cost")
    if cost is not None:
        try:
            return float(cost)
        except (TypeError, ValueError):
            pass
    p = prices or _FALLBACK_PRICE
    prompt = float(usage.get("prompt_tokens", 0) or 0)
    completion = float(usage.get("completion_tokens", 0) or 0)
    return prompt * p["prompt"] + completion * p["completion"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    agent_id          TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spend_ts ON spend (ts);
CREATE INDEX IF NOT EXISTS idx_spend_agent ON spend (agent_id);
"""


class SpendLedger:
    """Durable per-call spend record. One writer, many readers (WAL)."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SpendLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def record(self, agent_id: str, model: str, usage: dict) -> float:
        """Record one call from a provider usage dict; returns the cost added."""
        cost = cost_from_usage(usage)
        self._conn.execute(
            "INSERT INTO spend (ts, agent_id, model, prompt_tokens, completion_tokens, cost_usd)"
            " VALUES (?,?,?,?,?,?)",
            (time.time(), agent_id, model,
             int(usage.get("prompt_tokens", 0) or 0),
             int(usage.get("completion_tokens", 0) or 0), cost),
        )
        self._conn.commit()
        return cost

    def total(self) -> float:
        row = self._conn.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM spend").fetchone()
        return float(row["t"])

    def spent_since(self, ts: float) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS t FROM spend WHERE ts >= ?", (ts,)
        ).fetchone()
        return float(row["t"])

    def calls(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM spend").fetchone()["n"])

    def by_agent(self) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT agent_id, SUM(cost_usd) AS c FROM spend GROUP BY agent_id"
        ).fetchall()
        return {r["agent_id"]: float(r["c"]) for r in rows}

    def remaining(self, cap: Optional[float] = None) -> float:
        return (budget_cap() if cap is None else cap) - self.total()

    def over_cap(self, cap: Optional[float] = None) -> bool:
        return self.remaining(cap) <= 0

    def usage_sink(self, agent_id: str, model: str):
        """A callback for LLMClient(usage_sink=...) that records under this agent."""
        return lambda usage: self.record(agent_id, model, usage)


# -- the engine gate ----------------------------------------------------------


def activation_reached(now: Optional[date] = None) -> bool:
    today = now or datetime.now(timezone.utc).date()
    return today >= start_date()


def can_run(
    ledger: SpendLedger,
    cap: Optional[float] = None,
    now: Optional[date] = None,
) -> tuple[bool, str]:
    """Whether a tick may run. No fallback: at the cap, the pipeline stops."""
    if not activation_reached(now):
        return False, f"before activation date {start_date().isoformat()}"
    if ledger.over_cap(cap):
        c = budget_cap() if cap is None else cap
        return False, f"budget exhausted (spent ${ledger.total():.4f} of ${c:.2f} cap)"
    return True, "ok"
