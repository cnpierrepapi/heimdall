"""Ingest a generated catalog into DataHub, and garbage-collect it by id.

Each engine tick materialises a fresh `CatalogSpec` and pushes it into the live
DataHub instance so agents (and the console's deep-links) see a real catalog. The
catalog id is the db namespace of every URN, so an instance is isolated: nothing
outside it references its datasets, and the whole instance can be hard-deleted by
walking its own dataset URNs. That is what keeps the console's DataHub deep-links
from rotting when retention drops an old catalog.

`build_mcps(spec)` is a pure function of the spec (no network), so the shape of
what we ingest, the namespaced URNs, and the column-level lineage are all unit
testable offline. `ingest_spec` emits them; `hard_delete_catalog` removes them,
best effort per entity so a single degraded-ES delete cannot strand the tick
(see the ES timeseries 500 seen during earlier cleanups).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .catalog import CatalogSpec, spec_to_world


def _classes():
    """Lazy import of the DataHub SDK so importing this module stays cheap."""
    from datahub.emitter.mce_builder import make_data_platform_urn, make_schema_field_urn
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import (
        DatasetLineageTypeClass,
        DatasetPropertiesClass,
        FineGrainedLineageClass,
        FineGrainedLineageDownstreamTypeClass,
        FineGrainedLineageUpstreamTypeClass,
        OtherSchemaClass,
        SchemaFieldClass,
        SchemaFieldDataTypeClass,
        SchemaMetadataClass,
        StringTypeClass,
        UpstreamClass,
        UpstreamLineageClass,
    )
    return locals()


def catalog_dataset_urns(spec: CatalogSpec) -> list[str]:
    """Every dataset URN in this catalog instance (the GC target set)."""
    world = spec_to_world(spec)
    return [world.datasets[d.name].urn for d in spec.datasets]


def build_mcps(spec: CatalogSpec) -> list[Any]:
    """All MetadataChangeProposals to create this catalog: schema, props, lineage."""
    c = _classes()
    MCPW = c["MetadataChangeProposalWrapper"]
    world = spec_to_world(spec)
    platform_urn = c["make_data_platform_urn"](spec.platform)
    props = {"heimdall_catalog": spec.catalog}
    if spec.theme:
        props["heimdall_theme"] = spec.theme

    mcps: list[Any] = []
    for ds in spec.datasets:
        urn = world.datasets[ds.name].urn
        fields = [
            c["SchemaFieldClass"](
                fieldPath=col.name,
                type=c["SchemaFieldDataTypeClass"](type=c["StringTypeClass"]()),
                nativeDataType="text",
                description=col.description,  # None = undocumented, on purpose
            )
            for col in ds.columns
        ]
        mcps.append(MCPW(
            entityUrn=urn,
            aspect=c["SchemaMetadataClass"](
                schemaName=ds.name, platform=platform_urn, version=0, hash="",
                platformSchema=c["OtherSchemaClass"](rawSchema=""), fields=fields,
            ),
        ))
        mcps.append(MCPW(
            entityUrn=urn,
            aspect=c["DatasetPropertiesClass"](
                name=ds.name,
                description=f"{spec.theme or 'generated'} catalog {spec.catalog}: {ds.name}.",
                customProperties=dict(props),
            ),
        ))
        if ds.derived_from:
            upstream_names = sorted({up for up, _ in ds.derived_from.values()})
            upstreams = [
                c["UpstreamClass"](
                    dataset=world.datasets[up].urn,
                    type=c["DatasetLineageTypeClass"].TRANSFORMED,
                )
                for up in upstream_names
            ]
            fine = [
                c["FineGrainedLineageClass"](
                    upstreamType=c["FineGrainedLineageUpstreamTypeClass"].FIELD_SET,
                    upstreams=[c["make_schema_field_urn"](world.datasets[up].urn, up_col)],
                    downstreamType=c["FineGrainedLineageDownstreamTypeClass"].FIELD,
                    downstreams=[c["make_schema_field_urn"](urn, col)],
                )
                for col, (up, up_col) in sorted(ds.derived_from.items())
            ]
            mcps.append(MCPW(
                entityUrn=urn,
                aspect=c["UpstreamLineageClass"](upstreams=upstreams, fineGrainedLineages=fine),
            ))
    return mcps


def ingest_spec(spec: CatalogSpec, emitter: Any = None, gms_url: Optional[str] = None,
                with_pii_tags: bool = True) -> int:
    """Emit the catalog into DataHub. Returns the number of MCPs sent.

    PII tag entities are pre-created by default so a tagger's add_tags writes
    reference an existing tag rather than dangling.
    """
    if emitter is None:
        import os
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        emitter = DatahubRestEmitter(gms_url or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"))
    mcps = build_mcps(spec)
    if with_pii_tags:
        from .governance import pii_tag_mcps
        mcps = mcps + pii_tag_mcps()
    for mcp in mcps:
        emitter.emit(mcp)
    return len(mcps)


# -- garbage collection -------------------------------------------------------


@dataclass
class DeleteResult:
    urn: str
    ok: bool
    error: Optional[str] = None


def hard_delete_catalog(
    spec: CatalogSpec,
    graph: Any = None,
    gms_url: Optional[str] = None,
    deleter: Optional[Callable[[str], None]] = None,
) -> list[DeleteResult]:
    """Hard-delete every dataset in this catalog. Best effort, never raises.

    A `deleter(urn)` can be injected (tests, or a custom delete path); otherwise a
    DataHubGraph hard delete is used. One failed entity does not stop the rest, so
    a degraded-ES delete cannot strand the tick with a half-removed catalog.
    """
    if deleter is None:
        if graph is None:
            import os
            from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
            graph = DataHubGraph(DatahubClientConfig(
                server=gms_url or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")))
        deleter = lambda u: graph.delete_entity(u, hard=True)  # noqa: E731

    results: list[DeleteResult] = []
    for urn in catalog_dataset_urns(spec):
        try:
            deleter(urn)
            results.append(DeleteResult(urn, True))
        except Exception as exc:  # best effort: record and continue
            results.append(DeleteResult(urn, False, str(exc)))
    return results
