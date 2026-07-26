"""World evolution: the change is real, bounded, and honestly logged.

Evolution is what keeps a persistent world scoreable, so the properties that
matter are not "something happened" but: the same day replays identically, the
change is applied to the truth as well as to the log, nothing load-bearing is
destroyed, and only datasets that actually changed are pushed to DataHub.
"""

from __future__ import annotations

from heimdall.catalog import CatalogSpec, ColumnSpec, DatasetSpec, spec_to_world
from heimdall.evolve import (
    ADD_COLUMN,
    ADD_PII_COLUMN,
    ADD_TABLE,
    DOC_ROT,
    DROP_COLUMN,
    TABLE_CAP,
    _apply_one,
    _candidates,
    emit_delta,
    evolve_spec,
    plan_mutations,
)
from heimdall.generator import generate_catalog

SEED = 7


def _spec() -> CatalogSpec:
    return generate_catalog(3)


def _undocumented(spec: CatalogSpec) -> set[tuple[str, str]]:
    return {(d.name, c.name) for d in spec.datasets for c in d.columns
            if not c.description}


# -- determinism and replay ---------------------------------------------------


def test_the_same_day_replays_identically():
    """A live failure has to be reproducible offline, so the day is a function."""
    a, ma = evolve_spec(_spec(), day=4, seed=SEED)
    b, mb = evolve_spec(_spec(), day=4, seed=SEED)
    assert a.model_dump() == b.model_dump()
    assert [(m.kind, m.payload) for m in ma] == [(m.kind, m.payload) for m in mb]


def test_different_days_do_different_things():
    days = [tuple((m.kind, m.payload.get("column"), m.payload.get("dataset"))
                  for m in evolve_spec(_spec(), day=d, seed=SEED)[1])
            for d in range(1, 9)]
    assert len(set(days)) > 1, "a world that repeats one day forever is not evolving"


def test_the_input_spec_is_left_alone():
    """The caller persists the returned spec; a half-mutated original is a trap."""
    spec = _spec()
    before = spec.model_dump()
    evolve_spec(spec, day=2, seed=SEED)
    assert spec.model_dump() == before


# -- the change is real -------------------------------------------------------


def test_evolution_creates_new_undocumented_work():
    """The whole point: after a day of change there is more to do than before."""
    spec = _spec()
    before = _undocumented(spec)
    grown, mutations = evolve_spec(spec, day=2, seed=SEED, per_day=8)
    assert mutations
    assert _undocumented(grown) - before, "a day of change left no new work"


def test_a_new_column_arrives_undocumented_and_gradeable():
    spec = _spec()
    ds = spec.datasets[0].name
    m = _apply_one(spec, ADD_COLUMN, ds, ("refund_usd", ("refund", "usd")), day=2)
    col = next(c for c in spec.datasets[0].columns if c.name == "refund_usd")
    assert m is not None and m.datasets == (ds,)
    assert col.description is None, "arriving documented would create no work"
    assert col.gold_keywords, "work nothing can grade is not worth creating"


def test_a_new_pii_column_carries_its_pii_truth():
    spec = _spec()
    ds = spec.datasets[0].name
    _apply_one(spec, ADD_PII_COLUMN, ds,
               ("contact_phone", ("phone", "contact"), "phone"), day=2)
    col = next(c for c in spec.datasets[0].columns if c.name == "contact_phone")
    assert col.pii == "phone" and col.description is None


def test_doc_rot_hands_a_column_back_as_gradeable_work():
    """Rot takes the description, not the meaning, so the column stays scoreable."""
    spec = _spec()
    ds, col = next((d.name, c.name) for d in spec.datasets for c in d.columns
                   if c.description and not c.name.endswith(("_id", "_at")))
    m = _apply_one(spec, DOC_ROT, ds, col, day=2)
    rotted = next(c for c in next(d for d in spec.datasets if d.name == ds).columns
                  if c.name == col)
    assert m is not None
    assert rotted.description is None
    assert rotted.gold_keywords, "an ungradeable rot would only depress accepts"


def test_doc_rot_has_real_candidates_on_a_generated_catalog():
    """Guards the trap this nearly shipped with: a rule that matched nothing.

    The generator gives a column either a description or gold keywords, never
    both, so requiring both made doc rot silently impossible while the log looked
    healthy.
    """
    assert _candidates(_spec(), DOC_ROT), "doc rot must be reachable"


def test_every_mutation_kind_is_reachable_on_a_real_catalog():
    for kind in (ADD_COLUMN, ADD_PII_COLUMN, DROP_COLUMN, DOC_ROT, ADD_TABLE):
        assert _candidates(_spec(), kind), f"{kind} can never fire"


# -- nothing load-bearing is destroyed ----------------------------------------


def test_keys_and_timestamps_are_never_touched():
    spec = _spec()
    for kind in (DROP_COLUMN, DOC_ROT):
        for _, column in _candidates(spec, kind):
            assert not str(column).endswith(("_id", "_at")), f"{kind} targeted {column}"


def test_a_column_something_derives_from_is_never_dropped():
    spec = _spec()
    sources = {(up, up_col) for d in spec.datasets
               for up, up_col in d.derived_from.values()}
    for dataset, column in _candidates(spec, DROP_COLUMN):
        assert (dataset, column) not in sources


def test_dropping_a_column_leaves_no_dangling_lineage():
    """Lineage that points at a column that is gone is a broken world."""
    spec = CatalogSpec(catalog="c", datasets=[
        DatasetSpec(name="raw_a", columns=[
            ColumnSpec(name="a_id", description="key"),
            ColumnSpec(name="spare", description="d"),
        ]),
        DatasetSpec(name="stg_a", columns=[ColumnSpec(name="spare", description="d")],
                    derived_from={"spare": ("raw_a", "spare")}),
    ])
    m = _apply_one(spec, DROP_COLUMN, "raw_a", "spare", day=2)
    assert m is not None and set(m.datasets) == {"raw_a", "stg_a"}
    assert spec.datasets[1].derived_from == {}
    spec_to_world(spec)  # would raise on a dangling reference


def test_evolution_keeps_the_world_buildable_over_many_days():
    """Every day for a simulated season, and the world still resolves."""
    spec = _spec()
    for day in range(2, 60):
        spec, _ = evolve_spec(spec, day=day, seed=SEED)
        spec_to_world(spec)  # raises if any mutation broke the graph
    assert len(spec.datasets) <= TABLE_CAP


def test_table_growth_stops_at_the_cap():
    spec = _spec()
    spec.datasets += [
        DatasetSpec(name=f"filler_{i}", columns=[ColumnSpec(name="x_id", description="k")])
        for i in range(TABLE_CAP)
    ]
    assert _candidates(spec, ADD_TABLE) == []


def test_a_new_table_derives_only_from_datasets_that_exist():
    spec = _spec()
    source = next(d.name for d in spec.datasets if d.name.startswith("raw_"))
    m = _apply_one(spec, ADD_TABLE, source, None, day=5)
    assert m is not None
    spec_to_world(spec)
    added = next(d for d in spec.datasets if d.name == m.payload["dataset"])
    assert added.derived_from and all(up == source for up, _ in added.derived_from.values())


# -- the log tells the truth --------------------------------------------------


def test_only_applied_mutations_come_back():
    """A log entry for a change that did not happen is worse than no log."""
    spec = _spec()
    ds = spec.datasets[0].name
    taken = spec.datasets[0].columns[0].name
    assert _apply_one(spec, ADD_COLUMN, ds, (taken, ("x",)), day=2) is None
    assert _apply_one(spec, DOC_ROT, ds, "no_such_column", day=2) is None
    assert _apply_one(spec, DROP_COLUMN, "no_such_dataset", "x", day=2) is None


def test_planned_mutations_name_real_targets():
    spec = _spec()
    names = {d.name for d in spec.datasets}
    for kind, dataset, _ in plan_mutations(spec, day=3, seed=SEED, per_day=6):
        assert dataset in names, f"{kind} planned against a dataset that is not there"


# -- the delta is a delta -----------------------------------------------------


class _Recorder:
    def __init__(self):
        self.urns = []

    def emit(self, mcp):
        self.urns.append(mcp.entityUrn)


def test_only_changed_datasets_are_pushed_to_datahub():
    """A re-ingest would rewrite datasets nothing happened to."""
    spec = _spec()
    grown, mutations = evolve_spec(spec, day=2, seed=SEED, per_day=2)
    changed = {name for m in mutations for name in m.datasets}
    rec = _Recorder()
    emit_delta(grown, mutations, emitter=rec, graph=_NoAspect())

    world = spec_to_world(grown)
    touched = {u for u in rec.urns}
    expected = {world.datasets[n].urn for n in changed}
    assert touched == expected, "the delta reached datasets it had no business in"
    assert len(touched) < len(grown.datasets), "that is a re-ingest, not a delta"


def test_no_mutations_means_no_network_traffic():
    rec = _Recorder()
    assert emit_delta(_spec(), [], emitter=rec) == 0
    assert rec.urns == []


class _NoAspect:
    """A graph with no editable overlay to clear."""

    def get_aspect(self, urn, cls):
        return None

    def emit(self, mcp):
        raise AssertionError("nothing to clear, so nothing should be emitted")


def test_doc_rot_records_whether_it_cleared_the_overlay():
    """An agent-written description survives a schema write, so rot must say so.

    If the overlay is not cleared the column still looks documented to an agent,
    and the mutation would claim work it did not create.
    """
    spec = _spec()
    ds, col = next((d.name, c.name) for d in spec.datasets for c in d.columns
                   if c.description and not c.name.endswith(("_id", "_at")))
    m = _apply_one(spec, DOC_ROT, ds, col, day=2)
    emit_delta(spec, [m], emitter=_Recorder(), graph=_NoAspect())
    assert m.payload["overlay_cleared"] is False
