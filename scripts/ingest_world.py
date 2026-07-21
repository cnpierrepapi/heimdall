"""Ingest the simulator's lineworld graph into a DataHub instance.

Creates the 12 datasets with schemas, documented/undocumented columns, and
dataset plus column-level (fine-grained) lineage, so agents exploring via
the MCP server see exactly the world the simulator scores them against.

Run on the box: ~/dh/bin/python scripts/ingest_world.py
"""

from __future__ import annotations

import os

from datahub.emitter.mce_builder import make_data_platform_urn, make_schema_field_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
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

from heimdall.simulator.world import PLATFORM, build_default_world

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")


def main() -> None:
    world = build_default_world()
    emitter = DatahubRestEmitter(GMS_URL)
    platform_urn = make_data_platform_urn(PLATFORM)

    for ds in world.datasets.values():
        fields = [
            SchemaFieldClass(
                fieldPath=col.name,
                type=SchemaFieldDataTypeClass(type=StringTypeClass()),
                nativeDataType="text",
                description=col.description,  # None = undocumented, on purpose
            )
            for col in ds.columns
        ]
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=ds.urn,
                aspect=SchemaMetadataClass(
                    schemaName=ds.name,
                    platform=platform_urn,
                    version=0,
                    hash="",
                    platformSchema=OtherSchemaClass(rawSchema=""),
                    fields=fields,
                ),
            )
        )
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=ds.urn,
                aspect=DatasetPropertiesClass(
                    name=ds.name,
                    description=(
                        f"lineworld synthetic warehouse table ({ds.name}). "
                        "Part of the heimdall demo scenario."
                    ),
                ),
            )
        )

        if ds.derived_from:
            upstream_names = sorted({up for up, _ in ds.derived_from.values()})
            upstreams = [
                UpstreamClass(
                    dataset=world.datasets[up].urn,
                    type=DatasetLineageTypeClass.TRANSFORMED,
                )
                for up in upstream_names
            ]
            fine = [
                FineGrainedLineageClass(
                    upstreamType=FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                    upstreams=[
                        make_schema_field_urn(world.datasets[up].urn, up_col)
                    ],
                    downstreamType=FineGrainedLineageDownstreamTypeClass.FIELD,
                    downstreams=[make_schema_field_urn(ds.urn, col)],
                )
                for col, (up, up_col) in sorted(ds.derived_from.items())
            ]
            emitter.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=ds.urn,
                    aspect=UpstreamLineageClass(
                        upstreams=upstreams, fineGrainedLineages=fine
                    ),
                )
            )
        print(f"ingested {ds.name}")

    print(f"done: {len(world.datasets)} datasets into {GMS_URL}")


if __name__ == "__main__":
    main()
