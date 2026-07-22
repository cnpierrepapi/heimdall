"""T2: the seeded catalog generator and the theme library.

Pure, no-side-effect tests. They guard the two properties the engine relies on:
determinism (same seed -> identical catalog, so grounding and settlement agree)
and checkable truth (every column and every dataset carries something an
evaluator can grade), plus structural validity (acyclic lineage, unique namespace,
and full work-kind coverage on every instance).
"""

from __future__ import annotations

import re

from heimdall.catalog import spec_to_world
from heimdall.generator import THEMES, generate_catalog

SEEDS = list(range(600))


def test_determinism():
    for seed in (0, 1, 42, 7777, 123456):
        assert generate_catalog(seed) == generate_catalog(seed)


def test_catalog_id_shape_and_uniqueness():
    ids = {generate_catalog(s).catalog for s in SEEDS}
    for cid in ids:
        assert re.fullmatch(r"hcatalog_[0-9a-f]{8}", cid), cid
    # distinct seeds should overwhelmingly produce distinct namespaces
    assert len(ids) > len(SEEDS) * 0.9


def test_size_and_world_validity():
    for seed in SEEDS:
        spec = generate_catalog(seed)
        assert 6 <= len(spec.datasets) <= 10, (seed, len(spec.datasets))
        # spec_to_world runs the World constructor, which validates every
        # lineage reference; a bad derived_from would raise here.
        world = spec_to_world(spec)
        assert len(world.datasets) == len(spec.datasets)


def test_every_column_has_checkable_truth():
    for seed in SEEDS:
        spec = generate_catalog(seed)
        for ds in spec.datasets:
            for c in ds.columns:
                has_truth = bool(c.description) or bool(c.gold_keywords) or c.pii or c.term
                assert has_truth, f"{spec.catalog}.{ds.name}.{c.name} has no gradeable truth"


def test_lineage_is_acyclic():
    for seed in (0, 5, 99, 250, 599):
        world = spec_to_world(generate_catalog(seed))
        for name in world.datasets:
            assert name not in world.ancestors(name), f"{name} is its own ancestor"


def test_every_dataset_is_governed():
    for seed in SEEDS:
        spec = generate_catalog(seed)
        for ds in spec.datasets:
            assert ds.owner, f"{spec.catalog}.{ds.name} has no owner"
            assert ds.domain, f"{spec.catalog}.{ds.name} has no domain"
            assert ds.table_keywords, f"{spec.catalog}.{ds.name} has no table keyword"


def test_every_catalog_exercises_all_work_kinds():
    """PII, documentation, term-mapping and governance targets on every instance."""
    for seed in SEEDS:
        spec = generate_catalog(seed)
        cols = [c for ds in spec.datasets for c in ds.columns]
        assert any(c.pii for c in cols), f"{spec.catalog} has no PII target"
        assert any(c.term for c in cols), f"{spec.catalog} has no glossary term"
        assert any(c.description is None and c.gold_keywords for c in cols), \
            f"{spec.catalog} has no undocumented enricher target"


def test_pii_traps_present():
    """Identifier and geo columns must stay non-PII, or the PII checks are trivial."""
    for seed in (0, 3, 88, 400):
        spec = generate_catalog(seed)
        cols = {c.name: c for ds in spec.datasets for c in ds.columns}
        # every catalog carries a country_code that is a term, never PII
        assert "country_code" in cols
        assert cols["country_code"].pii is None
        assert cols["country_code"].term is not None


def test_theme_library_fully_covered():
    covered = {generate_catalog(s).theme for s in SEEDS}
    assert covered == {t.name for t in THEMES}
    assert len(THEMES) >= 12
