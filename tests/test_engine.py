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
    """Stub the tick at its two network edges and return what it ingests.

    Everything else runs for real: world selection, the ingest-once decision, the
    clock, grounding, settlement and the projection rebuild over empty stores.
    """
    monkeypatch.setenv("HEIMDALL_START_DATE", "2020-01-01")  # past the activation gate
    monkeypatch.setattr("heimdall.engine.health_ok", lambda c: (True, "healthy"))

    ingested: list[CatalogSpec] = []
    import heimdall.ingest as ing
    monkeypatch.setattr(ing, "ingest_spec",
                        lambda spec, gms_url=None, **kw: ingested.append(spec) or 0)
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
