"""T5: engine config, health gate, registry, and retention selection (offline).

The full tick needs live DataHub + an LLM, so it is proven on the box. Here we
lock the pure decision logic: paths, the health gate, the leaderboard registry,
and which catalogs retention hard-deletes.
"""

from __future__ import annotations

import os

from heimdall.catalog import CatalogSpec, DatasetSpec, save_spec
from heimdall.engine import EngineConfig, _retention_gc, health_ok, registry
from heimdall.roster import ROSTER


def test_config_paths_live_under_home(tmp_path):
    cfg = EngineConfig(home=str(tmp_path))
    for p in (cfg.events_db, cfg.findings_db, cfg.trust_db, cfg.spend_db, cfg.spec_dir):
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
