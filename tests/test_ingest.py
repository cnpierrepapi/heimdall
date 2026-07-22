"""T3: catalog ingestion MCPs and garbage collection, offline.

`build_mcps` and `hard_delete_catalog`'s urn selection are pure, so the shape of
what we push into DataHub and what GC targets are tested here without a live GMS.
The live ingest + delete round-trip is proven separately on the box.
"""

from __future__ import annotations

from heimdall.generator import generate_catalog
from heimdall.ingest import (
    DeleteResult,
    build_mcps,
    catalog_dataset_urns,
    hard_delete_catalog,
)


def _spec():
    return generate_catalog(42)


def test_urns_are_namespaced_and_unique():
    spec = _spec()
    urns = catalog_dataset_urns(spec)
    assert len(urns) == len(spec.datasets)
    assert len(set(urns)) == len(urns)
    for u in urns:
        assert spec.catalog in u
        assert spec.platform in u


def test_mcp_count_matches_datasets_and_lineage():
    spec = _spec()
    base = 2 * len(spec.datasets)  # schema + properties per dataset
    lineage = sum(1 for d in spec.datasets if d.derived_from)
    assert len(build_mcps(spec)) == base + lineage


def test_every_mcp_targets_this_catalog():
    spec = _spec()
    for mcp in build_mcps(spec):
        assert spec.catalog in mcp.entityUrn


def test_schema_fields_preserve_documentation_truth():
    """Undocumented raw columns must ingest with description None, not blank."""
    spec = _spec()
    from datahub.metadata.schema_classes import SchemaMetadataClass
    schemas = {
        m.entityUrn: m.aspect
        for m in build_mcps(spec)
        if isinstance(m.aspect, SchemaMetadataClass)
    }
    # find a raw dataset with at least one undocumented column in the spec
    raw = next(d for d in spec.datasets if d.name.startswith("raw_")
               and any(c.description is None for c in d.columns))
    urn = next(u for u in schemas if raw.name in u)
    by_name = {f.fieldPath: f for f in schemas[urn].fields}
    for col in raw.columns:
        assert by_name[col.name].description == col.description


def test_lineage_only_on_derived_datasets_and_stays_in_namespace():
    spec = _spec()
    from datahub.metadata.schema_classes import UpstreamLineageClass
    lineages = [m for m in build_mcps(spec) if isinstance(m.aspect, UpstreamLineageClass)]
    derived = {d.name for d in spec.datasets if d.derived_from}
    assert len(lineages) == len(derived)
    for m in lineages:
        for up in m.aspect.upstreams:
            assert spec.catalog in up.dataset  # never points outside the instance


def test_hard_delete_targets_all_datasets():
    spec = _spec()
    seen: list[str] = []
    results = hard_delete_catalog(spec, deleter=seen.append)
    assert seen == catalog_dataset_urns(spec)
    assert all(isinstance(r, DeleteResult) and r.ok for r in results)


def test_hard_delete_is_best_effort():
    """One failing entity must not stop the rest or raise."""
    spec = _spec()
    urns = catalog_dataset_urns(spec)
    bad = urns[1]

    def flaky(urn: str) -> None:
        if urn == bad:
            raise RuntimeError("ES timeseries delete 500")

    results = hard_delete_catalog(spec, deleter=flaky)
    assert len(results) == len(urns)
    failed = [r for r in results if not r.ok]
    assert len(failed) == 1 and failed[0].urn == bad
    assert "500" in failed[0].error
