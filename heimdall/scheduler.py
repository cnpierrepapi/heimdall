"""One scheduled run of the engine: tick, cut over once, publish, log.

This is what the systemd timer invokes every fifteen minutes. It wraps the tick
with the three things an unattended process needs and the tick itself does not do:

  * the cutover. The console shipped with a curated one-shot feed. The first tick
    that actually produces rows retires that feed so the engine becomes the sole
    source, and a sentinel file makes it happen exactly once. A tick that skips
    (dormant before the activation date, or unhealthy DataHub) must NOT cut over,
    or the console would be blanked with nothing to refill it.
  * publishing. The tick returns rows; the publisher writes them.
  * a single structured log line per run, and the guarantee that nothing raises.
    A timer unit that crashes still fires next interval, but the failure is then
    only visible in exit codes. One greppable line per run is what makes an
    unattended fortnight auditable after the fact.

Run it through scripts/engine_tick.py.
"""

from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .engine import EngineConfig, TickResult, load_config, run_tick

CUTOVER_SENTINEL = ".cutover_done"

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"


@dataclass
class RunLog:
    """The outcome of one scheduled run, as a status plus its log line."""

    status: str
    reason: str = ""
    line: str = ""
    published: dict[str, int] = field(default_factory=dict)
    cutover: bool = False


# -- cutover ------------------------------------------------------------------


def _sentinel_path(home: str) -> Path:
    return Path(home) / CUTOVER_SENTINEL


def cutover_done(home: str) -> bool:
    return _sentinel_path(home).exists()


def mark_cutover(home: str) -> None:
    p = _sentinel_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"showcase reset at {time.time():.0f}\n", encoding="utf-8")


# -- logging ------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    text = str(value)
    return f'"{text}"' if " " in text else text


def log_line(status: str, fields: dict[str, Any]) -> str:
    """A flat key=value line, greppable in journalctl."""
    parts = [f"status={status}"]
    parts += [f"{k}={_fmt(v)}" for k, v in fields.items() if v not in (None, "")]
    return " ".join(parts)


def _tick_fields(result: TickResult) -> dict[str, Any]:
    settle = result.settle or {}
    blocked = sum(s.blocked for s in result.stats)
    applied = sum(s.applied for s in result.stats)
    return {
        "catalog": result.catalog,
        "seed": result.seed,
        "agents": len(result.stats),
        "applied": applied,
        "blocked": blocked,
        "events": result.n_events,
        "findings": result.n_findings,
        "settled": settle.get("settled", 0),
        "accepted": settle.get("accepted", 0),
        "reverted": settle.get("reverted", 0),
        "board": len(result.agents),
        "spend_tick": result.spend_tick,
        "spend_total": result.spend_total,
        "gc": len(result.gc_deleted),
    }


# -- the scheduled run --------------------------------------------------------


def publish(result: TickResult, home: str,
            publisher_factory: Optional[Callable[[], Any]] = None) -> tuple[dict[str, int], bool]:
    """Cut over once if needed, then publish this tick's rows.

    Returns the published counts and whether this run performed the cutover. Only
    called for a tick that produced rows, which is what makes the cutover safe.
    """
    if publisher_factory is None:
        from .publisher import Publisher
        publisher_factory = Publisher

    cutover = False
    with publisher_factory() as pub:
        if not cutover_done(home):
            pub.reset_showcase()
            mark_cutover(home)
            cutover = True
        counts = pub.publish_tick(result)
    return counts, cutover


def run_once(cfg: Optional[EngineConfig] = None, seed: Optional[int] = None,
             publisher_factory: Optional[Callable[[], Any]] = None,
             publish_rows: bool = True) -> RunLog:
    """Run one tick and publish it. Never raises: the timer must survive anything."""
    cfg = cfg or load_config()
    started = time.time()
    try:
        result = run_tick(cfg, seed=seed)
    except Exception as exc:
        return RunLog(
            status=STATUS_ERROR, reason=str(exc)[:200],
            line=log_line(STATUS_ERROR, {"stage": "tick", "error": type(exc).__name__,
                                         "detail": str(exc)[:200]}),
        )

    if not result.ok:
        fields = {"reason": result.reason, "spend_total": result.spend_total,
                  "elapsed": round(time.time() - started, 1)}
        return RunLog(status=STATUS_SKIPPED, reason=result.reason,
                      line=log_line(STATUS_SKIPPED, fields))

    fields = _tick_fields(result)
    counts: dict[str, int] = {}
    cutover = False
    if publish_rows:
        try:
            counts, cutover = publish(result, cfg.home, publisher_factory)
            fields["published"] = ",".join(f"{k}:{v}" for k, v in counts.items())
            if cutover:
                fields["cutover"] = "showcase-reset"
        except Exception as exc:
            fields["publish_error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
            fields["elapsed"] = round(time.time() - started, 1)
            return RunLog(status=STATUS_ERROR, reason="publish failed",
                          line=log_line(STATUS_ERROR, fields))

    fields["elapsed"] = round(time.time() - started, 1)
    return RunLog(status=STATUS_OK, line=log_line(STATUS_OK, fields),
                  published=counts, cutover=cutover)


def main() -> int:
    """Entrypoint for the timer. Prints one line; exit code 0 unless the run errored."""
    publish_rows = os.environ.get("HEIMDALL_PUBLISH", "1") != "0"
    try:
        run = run_once(publish_rows=publish_rows)
    except Exception:  # last resort: a broken config must not crash the unit loop
        print(log_line(STATUS_ERROR, {"stage": "startup"}), flush=True)
        traceback.print_exc()
        return 1
    print(run.line, flush=True)
    return 1 if run.status == STATUS_ERROR else 0
