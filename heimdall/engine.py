"""The living-world tick: one non-overlapping cycle of the engine.

Every tick advances one persistent world by one simulated day, casts a subset of
the stable roster to work it through the gateway, grounds and settles what they
wrote into durable stores that accumulate across ticks, and rebuilds the console
projection. Because the stores persist and the same agents recur, trust
strengthens with n: the skill report is recomputed over all history every tick.

The worlds themselves persist too. A small roster is seeded once and never
deleted, which is what lets a claim made on one day settle on the next. Worlds
round-robin by whichever clock is furthest behind, so they age evenly.

Persisting alone would freeze the leaderboard, because a world whose columns are
all documented offers an honest agent nothing left to do. So each day after its
first the world also evolves: columns appear, documentation rots, tables arrive,
applied to DataHub as a delta on the affected datasets. Agents are pointed at the
raw layer plus whatever changed today, which is the work that is actually open.

The tick is defensive by construction. It takes a file lock so a slow tick blocks
the next rather than overlapping. It refuses to run before the activation date or
once the budget cap is reached (no fallback: the pipeline simply stops). It skips
on unhealthy DataHub rather than crashing. Retention still drains catalogs left
by the older churn engine; persistent worlds live in their own directory and are
out of its reach.

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
from .catalog import load_spec, spec_to_world
from .claims import ClaimStore
from .evolve import AppliedMutation, DEFAULT_PER_DAY, emit_delta, evolve_spec
from .grounding import FindingStore, WorldCatalogContext, ground_events
from .llm import DEFAULT_MODEL, LLMClient
from .mcp_client import DataHubMCP
from .observability import WRITE, EventStore
from .roster import CASTABLE_KINDS, KIND_PII, ROGUE, ROSTER, cast
from .snapshot import activity_rows, agents_rows, findings_rows
from .trust import SurfaceLedger, settle_observations
from .worldstore import BASE_SEED, DEFAULT_WORLDS, WorldStore, tick_seed

SHOWCASE = "showcase"


@dataclass
class EngineConfig:
    home: str
    gms_url: str = "http://localhost:8080"
    mcp_server: str = ""
    model: str = DEFAULT_MODEL
    cast_size: int = 4
    retention: int = 12
    worlds: int = DEFAULT_WORLDS
    # where the world roster is drawn from. changing it mints a different roster
    # under different urns, which is how a proof run gets a namespace of its own
    # rather than inheriting whatever a previous run left behind.
    base_seed: int = BASE_SEED
    mutations_per_day: int = DEFAULT_PER_DAY
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
        """Churn-engine catalogs. Retention drains this; no living world is here."""
        return os.path.join(self.home, "catalogs")

    @property
    def worlds_db(self) -> str:
        return os.path.join(self.home, "worlds.db")

    @property
    def worlds_dir(self) -> str:
        return os.path.join(self.home, "worlds")

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
        worlds=int(os.environ.get("HEIMDALL_WORLDS", str(DEFAULT_WORLDS))),
        base_seed=int(os.environ.get("HEIMDALL_BASE_SEED", str(BASE_SEED))),
        mutations_per_day=int(os.environ.get("HEIMDALL_MUTATIONS", str(DEFAULT_PER_DAY))),
    )


def registry() -> dict[str, dict[str, Any]]:
    """All roster agents are public showcase agents on the leaderboard."""
    return {a.agent_id: {"visibility": "public"} for a in ROSTER}


@dataclass
class TickResult:
    ok: bool
    reason: str = "ok"
    catalog: Optional[str] = None  # the world id worked this tick
    day: Optional[int] = None  # that world's simulated day after the advance
    ingested: bool = False  # True on a world's first ever tick
    seed: Optional[int] = None
    mutations: list[dict] = field(default_factory=list)  # what changed today
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
               enforce: bool = False, world_path: Optional[str] = None,
               teams: tuple[str, ...] = ()) -> RunStat:
    llm = LLMClient(model=cfg.model, usage_sink=spend.usage_sink(ragent.agent_id, cfg.model))
    try:
        with DataHubMCP(
            gms_url=cfg.gms_url, command=sys.executable, args=["-m", "heimdall.gateway"],
            extra_env=_gateway_env(cfg, ragent.agent_id, enforce, world_path),
        ) as mcp:
            return run_roster_agent(ragent, mcp, llm, dataset_urns, teams=teams)
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

    # pick the persistent world whose clock is furthest behind and work it
    store = WorldStore(cfg.worlds_db, cfg.worlds_dir)
    try:
        store.seed(cfg.worlds, base_seed=cfg.base_seed)  # once ever; then a no-op
        rec = store.next_world()
        if rec is None:
            return TickResult(ok=False, reason="no worlds seeded")
        spec = store.spec(rec.world_id)
        spec_path = store.spec_path(rec.world_id)

        # a world enters DataHub once. later ticks change it by delta, never by
        # re-ingest, so its URNs and the console's deep-links stay put.
        first_ingest = not rec.ingested
        if first_ingest:
            from .ingest import ingest_spec
            ingest_spec(spec, gms_url=cfg.gms_url)
            store.mark_ingested(rec.world_id)

        day = store.advance(rec.world_id)

        # a brand-new world is already all new work, so it is left alone on its
        # first day. after that the world has to change or the agents run out of
        # anything to be judged on and the leaderboard freezes while ticks burn.
        mutations: list[AppliedMutation] = []
        if not first_ingest and cfg.mutations_per_day > 0:
            spec, mutations = evolve_spec(spec, day, rec.seed,
                                          per_day=cfg.mutations_per_day)
            if mutations:
                emit_delta(spec, mutations, gms_url=cfg.gms_url)
                store.write_spec(spec)  # the gateway grounds against this file
                for m in mutations:
                    store.record_mutation(rec.world_id, day, m.kind, m.payload)
    finally:
        store.close()

    # rebuilt from the mutated spec, so a column added a moment ago is real to
    # the agents and to grounding. built from the stale spec, a new column would
    # be graded as one the schema does not have.
    world = spec_to_world(spec)
    changed = {name for m in mutations for name in m.datasets}
    targets = [world.datasets[d.name].urn for d in spec.datasets
               if d.name.startswith("raw_") or d.name in changed]

    # the cast follows the world's own clock, so a day can be replayed exactly
    seed = seed if seed is not None else tick_seed(rec.world_id, day)

    # cast: a seeded annotate subset plus one rogue PII tagger under enforce so the
    # feed carries held/blocked events when its over-tagging is caught in flight.
    # the catalog's own owner pool is the candidate list an owner agent picks from
    teams = tuple(sorted({d.owner for d in spec.datasets if d.owner}))
    annotate = cast(spec, seed, cfg.cast_size, kinds=CASTABLE_KINDS)
    enforce_agent = next((a for a in ROSTER if a.work_kind == KIND_PII and a.profile == ROGUE), None)
    if enforce_agent is not None:
        annotate = [a for a in annotate if a.agent_id != enforce_agent.agent_id]

    stats: list[RunStat] = []
    for ragent in annotate:
        runnable, _ = can_run(spend)
        if not runnable or spend.spent_since(tick_start) >= tick_subcap():
            break  # budget guard: stop casting, no fallback
        stats.append(_run_agent(cfg, ragent, spend, targets, teams=teams))
    if enforce_agent is not None and spend.spent_since(tick_start) < tick_subcap():
        runnable, _ = can_run(spend)
        if runnable:
            stats.append(_run_agent(cfg, enforce_agent, spend, targets,
                                    enforce=True, world_path=spec_path, teams=teams))

    # ground + settle this tick's observations into the durable stores
    events_store = EventStore(cfg.events_db)
    new_events = events_store.events(since_ts=tick_start)
    ctx = WorldCatalogContext(world)
    with FindingStore(cfg.findings_db) as fs:
        ground_events(new_events, ctx, fs)
        n_findings_tick = len([f for f in fs.findings() if f.ts >= tick_start])
    trust_store = ClaimStore(cfg.trust_db)
    # only work on artifacts that were new when the day began is scored, so the
    # ledger is built from the whole write history up to now. every write, not
    # only the ones that landed: a write the gateway blocked leaves the column
    # open for other agents, but the agent that tried has had its turn at it, and
    # filtering to landed writes here is what let a refused call be re-scored
    # every day. urns carry the catalog id, so other worlds cannot collide.
    prior = SurfaceLedger.as_of(events_store.events(op=WRITE), before_ts=tick_start)
    settle = settle_observations(new_events, ctx, trust_store, ledger=prior)

    # rebuild the console projection: this tick's activity + findings, full-history board
    activity = activity_rows(EventStore(cfg.events_db), owner=cfg.owner,
                             catalog=spec.catalog, since_ts=tick_start)
    with FindingStore(cfg.findings_db) as fs:
        findings = findings_rows(fs, owner=cfg.owner, catalog=spec.catalog, since_ts=tick_start)
        # conduct spans all history, like trust: both are the accumulated record
        agents = agents_rows(trust_store, registry=registry(), catalog=spec.catalog,
                             event_store=EventStore(cfg.events_db), finding_store=fs)

    gc = _retention_gc(cfg, keep_catalog=spec.catalog)

    return TickResult(
        ok=True, catalog=spec.catalog, day=day, ingested=first_ingest,
        seed=seed, stats=stats,
        mutations=[{"kind": m.kind, **m.payload} for m in mutations],
        n_events=len(new_events), n_findings=n_findings_tick, settle=settle,
        spend_tick=spend.spent_since(tick_start), spend_total=spend.total(),
        activity=activity, findings=findings, agents=agents, gc_deleted=gc,
    )
