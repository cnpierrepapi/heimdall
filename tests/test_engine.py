"""T5: engine config, health gate, registry, retention, and the tick's own choices.

Agent work needs live DataHub and an LLM, so that half is proven on the box. What
is locked here is the decision logic around it: paths, the health gate, the
leaderboard registry, which catalogs retention hard-deletes, and (W1) which world
a tick picks up, whether it ingests it, and how far it moves the clock. Those last
ones are stubbed at the network boundary rather than skipped, because the tick
choosing the wrong world is a silent failure that only shows up days later.
"""

from __future__ import annotations

import os

from heimdall.agentrun import RunStat
from heimdall.catalog import CatalogSpec, DatasetSpec, save_spec, spec_to_world
from heimdall.engine import EngineConfig, _retention_gc, health_ok, registry, run_tick
from heimdall.roster import ROSTER
from heimdall.trust import SurfaceLedger
from heimdall.worldstore import WorldStore


def test_config_paths_live_under_home(tmp_path):
    cfg = EngineConfig(home=str(tmp_path))
    for p in (cfg.events_db, cfg.findings_db, cfg.trust_db, cfg.spend_db, cfg.spec_dir,
              cfg.worlds_db, cfg.worlds_dir):
        assert p.startswith(str(tmp_path))


def test_registry_marks_every_agent_public():
    reg = registry()
    assert set(reg) == {a.agent_id for a in ROSTER}
    assert all(v["visibility"] == "public" for v in reg.values())


def test_health_fails_when_mcp_server_missing(tmp_path):
    cfg = EngineConfig(home=str(tmp_path), mcp_server=str(tmp_path / "nope"))
    ok, why = health_ok(cfg)
    assert not ok and "mcp server" in why


def _tiny_spec(catalog: str) -> CatalogSpec:
    return CatalogSpec(catalog=catalog, platform="postgres", theme="t",
                       datasets=[DatasetSpec(name="raw_x", columns=[])])


def test_retention_gc_deletes_oldest_beyond_window(tmp_path, monkeypatch):
    cfg = EngineConfig(home=str(tmp_path), retention=3)
    os.makedirs(cfg.spec_dir, exist_ok=True)
    # five catalogs, mtimes strictly increasing c0 (oldest) .. c4 (newest = kept)
    for i in range(5):
        p = os.path.join(cfg.spec_dir, f"c{i}.json")
        save_spec(_tiny_spec(f"c{i}"), p)
        os.utime(p, (1000 + i, 1000 + i))

    deleted_urns = []
    import heimdall.ingest as ing
    monkeypatch.setattr(ing, "hard_delete_catalog",
                        lambda spec, gms_url=None: deleted_urns.append(spec.catalog) or [])

    gone = _retention_gc(cfg, keep_catalog="c4")
    # retention window 3 keeps the 3 newest live catalogs (c2, c3, c4); c0, c1 go
    assert gone == ["c0", "c1"]
    assert not os.path.exists(os.path.join(cfg.spec_dir, "c0.json"))
    assert not os.path.exists(os.path.join(cfg.spec_dir, "c1.json"))
    assert os.path.exists(os.path.join(cfg.spec_dir, "c2.json"))
    assert os.path.exists(os.path.join(cfg.spec_dir, "c4.json"))


# -- W1: the tick against persistent worlds ------------------------------------


def _offline_tick(monkeypatch, cfg: EngineConfig) -> list[CatalogSpec]:
    """Stub the tick at its network edges and return what it ingests.

    Everything else runs for real: world selection, the ingest-once decision, the
    clock, which mutations the world gets, grounding, settlement and the projection
    rebuild over empty stores. The delta emitter is stubbed rather than the
    evolution itself, so the spec really does change under the tick.
    """
    monkeypatch.setenv("HEIMDALL_START_DATE", "2020-01-01")  # past the activation gate
    monkeypatch.setattr("heimdall.engine.health_ok", lambda c: (True, "healthy"))

    ingested: list[CatalogSpec] = []
    import heimdall.ingest as ing
    monkeypatch.setattr(ing, "ingest_spec",
                        lambda spec, gms_url=None, **kw: ingested.append(spec) or 0)
    monkeypatch.setattr("heimdall.engine.emit_delta",
                        lambda spec, muts, **kw: len(muts))
    monkeypatch.setattr(
        "heimdall.engine._run_agent",
        lambda cfg, ragent, spend, urns, **kw: RunStat(
            ragent.agent_id, ragent.work_kind, ragent.profile),
    )
    return ingested


def test_consecutive_ticks_work_the_same_world(tmp_path, monkeypatch):
    """The point of W1: tomorrow happens to the same catalog today happened to.

    Without this, no claim that needs time can ever settle, because the entity it
    was made about no longer exists.
    """
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    first = run_tick(cfg)
    second = run_tick(cfg)

    assert first.ok and second.ok
    assert first.catalog == second.catalog
    assert (first.day, second.day) == (1, 2)


def test_a_world_keeps_its_urns_across_ticks(tmp_path, monkeypatch):
    """Deep links published on day one must still resolve on day two."""
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    def urns() -> list[str]:
        with WorldStore(cfg.worlds_db, cfg.worlds_dir) as s:
            rec = s.next_world()
            world = spec_to_world(s.spec(rec.world_id))
            return sorted(d.urn for d in world.datasets.values())

    run_tick(cfg)
    before = urns()
    run_tick(cfg)
    assert urns() == before
    assert before, "a seeded world must own at least one dataset urn"


def test_a_world_is_ingested_exactly_once(tmp_path, monkeypatch):
    """Later ticks change a world by delta (W2), never by re-ingesting it."""
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    ingested = _offline_tick(monkeypatch, cfg)

    first = run_tick(cfg)
    second = run_tick(cfg)

    assert [s.catalog for s in ingested] == [first.catalog]
    assert first.ingested and not second.ingested


def test_ticks_rotate_across_worlds_before_repeating(tmp_path, monkeypatch):
    cfg = EngineConfig(home=str(tmp_path), worlds=3)
    ingested = _offline_tick(monkeypatch, cfg)

    catalogs = [run_tick(cfg).catalog for _ in range(4)]

    assert len(set(catalogs[:3])) == 3  # every world before any repeat
    assert catalogs[3] == catalogs[0]  # then round again, in the same order
    assert len(ingested) == 3  # each world ingested on its own first tick


def test_the_tick_never_writes_into_the_retention_directory(tmp_path, monkeypatch):
    """Persistence is protected by layout: GC globs `catalogs/`, worlds are not there."""
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    result = run_tick(cfg)

    assert os.listdir(cfg.spec_dir) == []
    assert os.path.exists(os.path.join(cfg.worlds_dir, f"{result.catalog}.json"))
    assert result.gc_deleted == []


# -- the world evolves, so the work never runs out ----------------------------


def _open_work(cfg: EngineConfig, catalog: str) -> set[tuple[str, str]]:
    """(dataset, column) pairs still undocumented in the world's own truth."""
    with WorldStore(cfg.worlds_db, cfg.worlds_dir) as s:
        spec = s.spec(catalog)
    return {(d.name, c.name) for d in spec.datasets for c in d.columns
            if not c.description}


def test_a_brand_new_world_is_not_mutated_on_its_first_day(tmp_path, monkeypatch):
    """Day one is already all new work; changing it would just be noise."""
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)
    assert run_tick(cfg).mutations == []


def test_later_days_change_the_world(tmp_path, monkeypatch):
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    run_tick(cfg)
    second = run_tick(cfg)

    assert second.mutations, "day two left the world exactly as it was"
    assert all(m["kind"] and m["day"] == 2 for m in second.mutations)


def test_evolution_is_recorded_in_the_mutation_log(tmp_path, monkeypatch):
    """The log is history an agent is invited to predict from, so it must match."""
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    run_tick(cfg)
    result = run_tick(cfg)

    with WorldStore(cfg.worlds_db, cfg.worlds_dir) as s:
        logged = s.mutations(world_id=result.catalog)
    assert [m.kind for m in logged] == [m["kind"] for m in result.mutations]
    assert all(m.day == 2 for m in logged)


def test_a_world_never_runs_out_of_work(tmp_path, monkeypatch):
    """The reason evolution exists.

    Documenting is one way work leaves the queue, so the queue has to be refilled
    or the leaderboard freezes while ticks keep burning budget. Here nothing is
    documented at all, so this checks the supply side alone: change keeps arriving.
    """
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    first = run_tick(cfg)
    start = _open_work(cfg, first.catalog)
    for _ in range(8):
        run_tick(cfg)
    later = _open_work(cfg, first.catalog)

    assert later - start, "eight simulated days produced no new work"


def test_evolution_never_moves_an_existing_dataset(tmp_path, monkeypatch):
    """A settled claim and a published console link both point at a urn."""
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    def urns() -> set[str]:
        with WorldStore(cfg.worlds_db, cfg.worlds_dir) as s:
            world = spec_to_world(s.spec(s.next_world().world_id))
        return {d.urn for d in world.datasets.values()}

    run_tick(cfg)
    day_one = urns()
    for _ in range(6):
        run_tick(cfg)

    assert day_one <= urns(), "a urn published on day one stopped resolving"


def test_the_world_is_only_ingested_once_however_much_it_changes(tmp_path, monkeypatch):
    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    ingested = _offline_tick(monkeypatch, cfg)
    for _ in range(5):
        run_tick(cfg)
    assert len(ingested) == 1, "the world was re-ingested instead of changed by delta"


def test_the_tick_hands_scoring_every_write_not_only_the_ones_that_landed(
        tmp_path, monkeypatch):
    """A blocked write must reach the surface ledger.

    It never lands, so it leaves the column open for other agents, but the agent
    that tried has had its turn. Filtering the ledger's history to landed writes
    is what let one refused call be re-scored every simulated day, and no unit
    test below this seam could see it: the filter was in the query.
    """
    from heimdall.observability import BLOCKED, WRITE, EventStore, ObservationEvent

    cfg = EngineConfig(home=str(tmp_path), worlds=1)
    _offline_tick(monkeypatch, cfg)

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,x.raw_y,PROD)"
    EventStore(cfg.events_db).record(ObservationEvent(
        agent_id="orion-pii", tool="add_tags", op=WRITE, status=BLOCKED, ts=1.0,
        args={"entity_urns": [urn], "column_paths": ["c"],
              "tag_urns": ["urn:li:tag:pii-email"]}))

    seen: list = []
    real = SurfaceLedger.as_of
    monkeypatch.setattr(SurfaceLedger, "as_of",
                        classmethod(lambda cls, events, before_ts=None:
                                    seen.append(list(events)) or real(events, before_ts)))

    run_tick(cfg)

    statuses = {e.status for batch in seen for e in batch}
    assert BLOCKED in statuses, "the ledger never saw the blocked attempt"
