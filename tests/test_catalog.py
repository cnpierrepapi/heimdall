"""T1: CatalogSpec serialisation and the World <-> spec round-trip.

These are pure, no-side-effect tests. They guard the contract that a catalog can
be serialised, written to disk, reloaded, and materialised without the evaluators
seeing anything different, and that generated instances get unique URN namespaces.
"""

from __future__ import annotations

from heimdall.catalog import (
    CatalogSpec,
    load_spec,
    load_world,
    save_spec,
    spec_to_world,
    world_catalog_context,
    world_to_spec,
)
from heimdall.grounding import ground_action
from heimdall.simulator.world import build_default_world, dataset_urn


def _default_spec() -> CatalogSpec:
    return world_to_spec(build_default_world(), catalog="lineworld")


def test_world_to_spec_covers_every_dataset_and_column():
    world = build_default_world()
    spec = world_to_spec(world, catalog="lineworld", theme="warehouse", seed=7)
    assert spec.catalog == "lineworld"
    assert spec.theme == "warehouse"
    assert spec.seed == 7
    assert len(spec.datasets) == len(world.datasets)
    for ds in spec.datasets:
        assert ds.columns, f"{ds.name} lost its columns"


def test_round_trip_preserves_grounding_truth():
    """world -> spec -> world keeps every fact an evaluator reads."""
    original = build_default_world()
    rebuilt = spec_to_world(world_to_spec(original, catalog="lineworld"))

    assert set(rebuilt.datasets) == set(original.datasets)
    for name, od in original.datasets.items():
        rd = rebuilt.datasets[name]
        assert [c.name for c in rd.columns] == [c.name for c in od.columns]
        assert rd.owner == od.owner
        assert rd.domain == od.domain
        assert rd.table_keywords == od.table_keywords
        assert rd.derived_from == od.derived_from
        for oc, rc in zip(od.columns, rd.columns):
            assert rc.description == oc.description
            assert rc.gold_keywords == oc.gold_keywords
            assert rc.pii == oc.pii
            assert rc.term == oc.term


def test_default_namespace_round_trips_to_identical_urns():
    """A lineworld round-trip must reproduce the original URNs exactly."""
    original = build_default_world()
    rebuilt = spec_to_world(world_to_spec(original, catalog="lineworld"))
    for name in original.datasets:
        assert rebuilt.datasets[name].urn == original.datasets[name].urn
        assert rebuilt.datasets[name].urn == dataset_urn(name)


def test_json_serialisation_round_trips():
    spec = _default_spec()
    reloaded = CatalogSpec.model_validate_json(spec.model_dump_json())
    assert reloaded == spec


def test_generated_instance_gets_unique_namespace():
    """A distinct catalog id must yield distinct URNs that still resolve."""
    spec = _default_spec()
    spec.catalog = "hcatalog_ab12cd34"
    spec.platform = "snowflake"
    world = spec_to_world(spec)
    ctx = world_catalog_context_from_spec(spec)

    urn = world.datasets["raw_orders"].urn
    assert "hcatalog_ab12cd34" in urn
    assert "snowflake" in urn
    assert urn != dataset_urn("raw_orders")  # does not collide with lineworld
    assert ctx.dataset_name(urn) == "raw_orders"


def test_save_and_load_spec(tmp_path):
    spec = _default_spec()
    spec.catalog = "hcatalog_deadbeef"
    path = tmp_path / "nested" / "spec.json"
    save_spec(spec, path)
    assert path.exists()
    assert load_spec(path) == spec

    world = load_world(path)
    assert "hcatalog_deadbeef" in world.datasets["raw_orders"].urn


def test_loaded_context_grounds_an_action(tmp_path):
    """The gateway's loader path must produce a context the evaluators can use."""
    spec = _default_spec()
    spec.catalog = "hcatalog_feedface"
    path = tmp_path / "spec.json"
    save_spec(spec, path)

    ctx = world_catalog_context(path)
    urn = spec_to_world(spec).datasets["raw_orders"].urn
    # ghost_column is not in raw_orders -> an undefined_column finding
    findings = ground_action(
        "agent-x", "update_description",
        {"entity_urn": urn, "column_path": "ghost_column", "description": "x"},
        ctx,
    )
    assert any(f.check_type == "undefined_column" for f in findings)


def world_catalog_context_from_spec(spec: CatalogSpec):
    """Small helper: a context straight off an in-memory spec (no disk)."""
    from heimdall.grounding import WorldCatalogContext
    return WorldCatalogContext(spec_to_world(spec))
