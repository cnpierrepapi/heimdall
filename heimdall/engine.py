"""The living-catalog tick: one non-overlapping cycle of the engine.

Every tick generates a fresh unique catalog, ingests it into DataHub, casts a
subset of the stable roster to work it through the gateway, grounds and settles
what they wrote into durable stores that accumulate across ticks, and rebuilds the
console projection. Because the stores persist and the same agents recur, trust
strengthens with n: the skill report is recomputed over all history every tick.

The tick is defensive by construction. It takes a file lock so a slow tick blocks
the next rather than overlapping. It refuses to run before the activation date or
once the budget cap is reached (no fallback: the pipeline simply stops). It skips
on unhealthy DataHub rather than crashing. Retention hard-deletes catalogs older
than the window so the console's DataHub deep-links never rot.

Publishing the rebuilt rows to Supabase is the publisher's job (T6); this module
produces the rows and returns them.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .agentrun import RunStat, run_roster_agent
from .budget import SpendLedger, can_run, tick_subcap
from .catalog import CatalogSpec, load_spec, save_spec, spec_to_world
from .claims import ClaimStore
from .generator import generate_catalog
from .grounding import FindingStore, WorldCatalogContext, ground_events
from .llm import DEFAULT_MODEL, LLMClient
from .mcp_client import DataHubMCP
from .observability import EventStore
from .roster import CASTABLE_KINDS, KIND_PII, ROGUE, ROSTER, cast
from .snapshot import activity_rows, agents_rows, findings_rows
from .trust import settle_observations, trust_report

SHOWCASE = "showcase"


@dataclass
class EngineConfig:
    home: str
    gms_url: str = "http://localhost:8080"
    mcp_server: str = ""
    model: str = DEFAULT_MODEL
    cast_size: int = 4
    retention: int = 12
    owner: str = SHOWCASE

    @property
    def events_db(self) -> str:
        return os.path.join(self.home, "events.db")

    @property
    def findings_db(self) -> str:
        return os.path.join(self.home, "findings.db")

    @property
    def trust_db(self) -> str:
        return os.path.join(self.home, "trust.db")

    @property
    def spend_db(self) -> str:
        return os.path.join(self.home, "spend.db")

    @property
    def gateway_db(self) -> str:
        return os.path.join(self.home, "gateway.db")

    @property
    def spec_dir(self) -> str:
        return os.path.join(self.home, "catalogs")

    @property
    def lock_path(self) -> str:
        return os.path.join(self.home, "tick.lock")


def load_config() -> EngineConfig:
    home = os.environ.get("HEIMDALL_ENGINE_HOME", os.path.expanduser("~/.heimdall/engine"))
    return EngineConfig(
        home=home,
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        mcp_server=os.environ.get("MCP_SERVER_DATAHUB", ""),
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        cast_size=int(os.environ.get("HEIMDALL_CAST_SIZE", "4")),
        retention=int(os.environ.get("HEIMDALL_RETENTION", "12")),
    )


def registry() -> dict[str, dict[str, Any]]:
    """All roster agents are public showcase agents on the leaderboard."""
    return {a.agent_id: {"visibility": "public"} for a in ROSTER}


@dataclass
class TickResult:
    ok: bool
    reason: str = "ok"
    catalog: Optional[str] = None
    seed: Optional[int] = None
    stats: list[RunStat] = field(default_factory=list)
    n_events: int = 0
    n_findings: int = 0
    settle: dict = field(default_factory=dict)
    spend_tick: float = 0.0
    spend_total: float = 0.0
    activity: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    gc_deleted: list[str] = field(default_factory=list)


# -- lock and health ----------------------------------------------------------


@contextlib.contextmanager
def _tick_lock(path: str):
    """Best-effort non-overlap lock (POSIX flock). Yields True if acquired."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # non-POSIX: no lock available, proceed
        yield True
        return
    fh = open(path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()


def health_ok(cfg: EngineConfig) -> tuple[bool, str]:
    if cfg.mcp_server and not os.path.exists(cfg.mcp_server):
        return False, f"mcp server missing at {cfg.mcp_server}"
    try:
        import httpx
        r = httpx.get(f"{cfg.gms_url}/health", timeout=10)
        if r.status_code != 200:
            return False, f"GMS health {r.status_code}"
    except Exception as exc:
        return False, f"GMS unreachable: {str(exc)[:80]}"
    return True, "healthy"


# -- agent execution ----------------------------------------------------------


def _gateway_env(cfg: EngineConfig, agent_id: str, enforce: bool,
                 world_path: Optional[str]) -> dict[str, str]:
    env = {
        "HEIMDALL_AGENT_ID": agent_id,
        "HEIMDALL_EVENTS": cfg.events_db,
        "LEDGER_DB": cfg.gateway_db,
        "HEIMDALL_POLICY": "enforce" if enforce else "annotate",
        "HEIMDALL_CATALOG": "world",
        "MCP_SERVER_DATAHUB": cfg.mcp_server,
        "DATAHUB_GMS_URL": cfg.gms_url,
    }
    if world_path:
        env["HEIMDALL_WORLD_PATH"] = world_path
    return env


def _run_agent(cfg: EngineConfig, ragent, spend: SpendLedger, dataset_urns: list[str],
               enforce: bool = False, world_path: Optional[str] = None) -> RunStat:
    llm = LLMClient(model=cfg.model, usage_sink=spend.usage_sink(ragent.agent_id, cfg.model))
    try:
        with DataHubMCP(
            gms_url=cfg.gms_url, command=sys.executable, args=["-m", "heimdall.gateway"],
            extra_env=_gateway_env(cfg, ragent.agent_id, enforce, world_path),
        ) as mcp:
            return run_roster_agent(ragent, mcp, llm, dataset_urns)
    finally:
        llm.close()


# -- retention ----------------------------------------------------------------


def _retention_gc(cfg: EngineConfig, keep_catalog: str) -> list[str]:
    """Hard-delete DataHub catalogs older than the window; drop their spec files."""
    from .ingest import hard_delete_catalog
    specs = sorted(Path(cfg.spec_dir).glob("*.json"), key=lambda p: p.stat().st_mtime)
    # never GC the catalog we just built, regardless of window
    specs = [p for p in specs if p.stem != keep_catalog]
    excess = len(specs) + 1 - cfg.retention  # +1 for the just-built catalog
    deleted: list[str] = []
    for p in specs[:max(0, excess)]:
        try:
            hard_delete_catalog(load_spec(p), gms_url=cfg.gms_url)
            p.unlink()
            deleted.append(p.stem)
        except Exception:
            continue  # best effort; a failed GC does not stop the tick
    return deleted


# -- the tick -----------------------------------------------------------------


def run_tick(cfg: EngineConfig, seed: Optional[int] = None) -> TickResult:
    with _tick_lock(cfg.lock_path) as acquired:
        if not acquired:
            return TickResult(ok=False, reason="another tick is running")
        return _tick_body(cfg, seed)


def _tick_body(cfg: EngineConfig, seed: Optional[int]) -> TickResult:
    Path(cfg.spec_dir).mkdir(parents=True, exist_ok=True)
    spend = SpendLedger(cfg.spend_db)

    ok, why = can_run(spend)
    if not ok:
        return TickResult(ok=False, reason=why, spend_total=spend.total())
    ok, why = health_ok(cfg)
    if not ok:
        return TickResult(ok=False, reason=f"unhealthy: {why}")

    tick_start = time.time()
    seed = seed if seed is not None else int(tick_start)
    spec = generate_catalog(seed)
    spec_path = os.path.join(cfg.spec_dir, f"{spec.catalog}.json")
    save_spec(spec, spec_path)
    world = spec_to_world(spec)
    raw_urns = [world.datasets[d.name].urn for d in spec.datasets if d.name.startswith("raw_")]

    from .ingest import ingest_spec
    ingest_spec(spec, gms_url=cfg.gms_url)

    # cast: a seeded annotate subset plus one rogue PII tagger under enforce so the
    # feed carries held/blocked events when its over-tagging is caught in flight.
    annotate = cast(spec, seed, cfg.cast_size, kinds=CASTABLE_KINDS)
    enforce_agent = next((a for a in ROSTER if a.work_kind == KIND_PII and a.profile == ROGUE), None)
    if enforce_agent is not None:
        annotate = [a for a in annotate if a.agent_id != enforce_agent.agent_id]

    stats: list[RunStat] = []
    for ragent in annotate:
        runnable, _ = can_run(spend)
        if not runnable or spend.spent_since(tick_start) >= tick_subcap():
            break  # budget guard: stop casting, no fallback
        stats.append(_run_agent(cfg, ragent, spend, raw_urns))
    if enforce_agent is not None and spend.spent_since(tick_start) < tick_subcap():
        runnable, _ = can_run(spend)
        if runnable:
            stats.append(_run_agent(cfg, enforce_agent, spend, raw_urns,
                                    enforce=True, world_path=spec_path))

    # ground + settle this tick's observations into the durable stores
    new_events = EventStore(cfg.events_db).events(since_ts=tick_start)
    ctx = WorldCatalogContext(world)
    with FindingStore(cfg.findings_db) as fs:
        ground_events(new_events, ctx, fs)
        n_findings_tick = len([f for f in fs.findings() if f.ts >= tick_start])
    trust_store = ClaimStore(cfg.trust_db)
    settle = settle_observations(new_events, ctx, trust_store)

    # rebuild the console projection: this tick's activity + findings, full-history board
    activity = activity_rows(EventStore(cfg.events_db), owner=cfg.owner,
                             catalog=spec.catalog, since_ts=tick_start)
    with FindingStore(cfg.findings_db) as fs:
        findings = findings_rows(fs, owner=cfg.owner, catalog=spec.catalog, since_ts=tick_start)
    agents = agents_rows(trust_store, registry=registry(), catalog=spec.catalog)

    gc = _retention_gc(cfg, keep_catalog=spec.catalog)

    return TickResult(
        ok=True, catalog=spec.catalog, seed=seed, stats=stats,
        n_events=len(new_events), n_findings=n_findings_tick, settle=settle,
        spend_tick=spend.spent_since(tick_start), spend_total=spend.total(),
        activity=activity, findings=findings, agents=agents, gc_deleted=gc,
    )
