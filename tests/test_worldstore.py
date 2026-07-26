"""W1: the worlds persist, and their clocks are the only thing that moves.

The churn engine could be wrong about a catalog and nobody would ever know,
because the catalog was gone by the next tick. A persistent world has the
opposite failure mode: a mistake compounds for the rest of the season. So the
properties worth locking are the ones that keep the roster stable and its history
honest. The roster is reproducible and seeded exactly once; a world's identity
never moves once it exists; the clock only ever goes forward; and the history an
agent is allowed to read stops short of the day it is being asked to predict.
"""

from __future__ import annotations

import pytest

from heimdall.worldstore import (
    DEFAULT_WORLDS,
    WorldStore,
    seed_specs,
    tick_seed,
)


def store(tmp_path, name="worlds") -> WorldStore:
    return WorldStore(tmp_path / f"{name}.db", tmp_path / name)


# -- seeding ------------------------------------------------------------------


def test_seeding_gives_distinct_themes_and_ids(tmp_path):
    s = store(tmp_path)
    recs = s.seed(3)
    assert len(recs) == 3
    assert len({r.theme for r in recs}) == 3
    assert len({r.world_id for r in recs}) == 3
    # a fresh world has not run and has not been ingested
    assert all(r.day == 0 and not r.ingested for r in recs)


def test_seeding_is_reproducible_across_independent_stores(tmp_path):
    a = store(tmp_path, "a").seed(DEFAULT_WORLDS)
    b = store(tmp_path, "b").seed(DEFAULT_WORLDS)
    assert [r.world_id for r in a] == [r.world_id for r in b]
    assert [r.theme for r in a] == [r.theme for r in b]


def test_seeding_a_second_time_changes_nothing_even_with_a_bigger_k(tmp_path):
    """Growing the roster must be a deliberate act, not a config bump.

    A fourth world seeded mid-season would enter at day 0 beside worlds with a
    long history, and the shrinkage in the trust model would read that youth as
    low trust. So `seed` is blind to `k` once any world exists.
    """
    s = store(tmp_path)
    first = s.seed(3)
    assert [r.world_id for r in s.seed(7)] == [r.world_id for r in first]
    assert len(s.worlds()) == 3


def test_seeding_survives_a_reopened_store(tmp_path):
    s = store(tmp_path)
    first = s.seed(3)
    s.close()
    again = store(tmp_path).seed(3)
    assert [r.world_id for r in again] == [r.world_id for r in first]


def test_seeding_refuses_more_worlds_than_there_are_themes(tmp_path):
    with pytest.raises(ValueError, match="distinct themes"):
        seed_specs(99)


def test_seeding_refuses_a_roster_of_none(tmp_path):
    with pytest.raises(ValueError, match="at least one world"):
        seed_specs(0)


# -- specs on disk ------------------------------------------------------------


def test_spec_round_trips_under_the_world_id(tmp_path):
    s = store(tmp_path)
    for rec in s.seed(3):
        spec = s.spec(rec.world_id)
        assert spec.catalog == rec.world_id  # the id IS the URN namespace
        assert spec.theme == rec.theme
        assert spec.datasets


def test_specs_live_outside_the_churn_catalog_directory(tmp_path):
    """Retention only ever globs `catalogs/`, so a world must not live there."""
    s = store(tmp_path)
    rec = s.seed(1)[0]
    assert "catalogs" not in s.spec_path(rec.world_id)
    assert str(s.worlds_dir) in s.spec_path(rec.world_id)


def test_write_spec_overwrites_in_place(tmp_path):
    """W2 mutates a world by rewriting its spec; the path must not move."""
    s = store(tmp_path)
    rec = s.seed(1)[0]
    spec = s.spec(rec.world_id)
    before = s.spec_path(rec.world_id)
    spec.datasets = spec.datasets[:1]
    assert s.write_spec(spec) == before
    assert len(s.spec(rec.world_id).datasets) == 1


# -- selection and the clock --------------------------------------------------


def test_next_world_is_none_before_seeding(tmp_path):
    assert store(tmp_path).next_world() is None


def test_rotation_ages_every_world_evenly(tmp_path):
    s = store(tmp_path)
    ids = [r.world_id for r in s.seed(3)]
    picked = []
    for _ in range(6):
        rec = s.next_world()
        picked.append(rec.world_id)
        s.advance(rec.world_id)
    assert sorted(picked) == sorted(ids * 2)
    assert picked[:3] == picked[3:]  # a stable order, not an arbitrary one
    assert {w.day for w in s.worlds()} == {2}


def test_a_world_that_never_advanced_is_picked_again(tmp_path):
    """Crash safety: a tick that dies before the advance must not skip a world.

    The cursor is derived from the days themselves rather than stored, so an
    interrupted tick leaves that world still furthest behind.
    """
    s = store(tmp_path)
    s.seed(3)
    first = s.next_world()
    s.mark_ingested(first.world_id)  # got as far as ingest, then died
    assert s.next_world().world_id == first.world_id


def test_advance_only_moves_forward_and_returns_the_new_day(tmp_path):
    s = store(tmp_path)
    rec = s.seed(1)[0]
    assert s.advance(rec.world_id) == 1
    assert s.advance(rec.world_id) == 2
    assert s.get(rec.world_id).day == 2


def test_advance_rejects_an_unknown_world(tmp_path):
    with pytest.raises(KeyError):
        store(tmp_path).advance("hcatalog_nope")


# -- ingest flag --------------------------------------------------------------


def test_ingest_is_recorded_per_world_and_sticks(tmp_path):
    """A world enters DataHub once; later ticks change it by delta, not re-ingest."""
    s = store(tmp_path)
    recs = s.seed(3)
    s.mark_ingested(recs[0].world_id)
    assert s.get(recs[0].world_id).ingested
    assert not s.get(recs[1].world_id).ingested
    # advancing the clock does not clear it
    s.advance(recs[0].world_id)
    assert s.get(recs[0].world_id).ingested


def test_mark_ingested_rejects_an_unknown_world(tmp_path):
    with pytest.raises(KeyError):
        store(tmp_path).mark_ingested("hcatalog_nope")


# -- the mutation log ---------------------------------------------------------


def test_a_fresh_world_has_no_history(tmp_path):
    s = store(tmp_path)
    rec = s.seed(1)[0]
    assert s.mutations(rec.world_id) == []


def test_mutations_read_back_in_day_order_with_their_payload(tmp_path):
    s = store(tmp_path)
    w = s.seed(1)[0].world_id
    s.record_mutation(w, 2, "drop_column", {"dataset": "raw_x", "column": "c"})
    s.record_mutation(w, 1, "add_column", {"dataset": "raw_x"})
    s.record_mutation(w, 1, "doc_rot", {"dataset": "raw_y"})
    got = s.mutations(w)
    assert [m.kind for m in got] == ["add_column", "doc_rot", "drop_column"]
    assert got[2].payload == {"dataset": "raw_x", "column": "c"}


def test_history_stops_short_of_the_day_being_predicted(tmp_path):
    """This is the whole point of the log: it is evidence, not an answer key.

    An agent forecasting day 3 may read days 0 to 2. Handing it day 3 would make
    the forecast a lookup and the resulting trust score a lie.
    """
    s = store(tmp_path)
    w = s.seed(1)[0].world_id
    for day in range(5):
        s.record_mutation(w, day, "load_arrived", {"late": day % 2 == 0})
    seen = s.mutations(w, before_day=3)
    assert [m.day for m in seen] == [0, 1, 2]


def test_history_is_per_world_and_filterable_by_kind(tmp_path):
    s = store(tmp_path)
    a, b = [r.world_id for r in s.seed(2)]
    s.record_mutation(a, 1, "add_column", {})
    s.record_mutation(a, 1, "doc_rot", {})
    s.record_mutation(b, 1, "add_column", {})
    assert len(s.mutations(a)) == 2
    assert len(s.mutations(b)) == 1
    assert [m.world_id for m in s.mutations(a, kind="add_column")] == [a]


# -- the tick seed ------------------------------------------------------------


def test_tick_seed_is_a_pure_function_of_world_and_day(tmp_path):
    """Replaying a day must cast the same agents, so a live failure reproduces."""
    assert tick_seed("hcatalog_a", 5) == tick_seed("hcatalog_a", 5)
    assert tick_seed("hcatalog_a", 5) != tick_seed("hcatalog_a", 6)
    assert tick_seed("hcatalog_a", 5) != tick_seed("hcatalog_b", 5)
