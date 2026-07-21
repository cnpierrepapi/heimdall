"""Feasibility probe: can a DataHub catalog's timeline span 100+ days?

Two questions decide whether reconstruction is viable on a demo catalog:
  Q1 (time)     does the Timeline honor a backdated audit timestamp, so we can
                seed history that spans 100+ days instead of one session?
  Q2 (identity) can writes be attributed to distinct actors, so the history has
                more than one writer to score and real cross-actor reverts?

This emits a couple of backdated documentation changes on a throwaway dataset
via the SDK, trying a custom audit stamp (time + actor), then reads the timeline
back and prints what timestamps and actors actually landed. Run on the box.
"""

import time

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    AuditStampClass,
    EditableSchemaFieldInfoClass,
    EditableSchemaMetadataClass,
    OtherSchemaClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
    SystemMetadataClass,
)

from heimdall.provenance import fetch_timeline, parse_timeline

GMS = "http://localhost:8080"
DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.__backdate_test,PROD)"
FIELD = "amount"
DAY_MS = 86_400_000


def emit_desc(emitter, description, days_ago, actor):
    ts = int(time.time() * 1000) - days_ago * DAY_MS
    stamp = AuditStampClass(time=ts, actor=actor)
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=DS,
            aspect=EditableSchemaMetadataClass(
                created=stamp,
                lastModified=stamp,
                editableSchemaFieldInfo=[
                    EditableSchemaFieldInfoClass(fieldPath=FIELD, description=description)
                ],
            ),
            systemMetadata=SystemMetadataClass(lastObserved=ts),
        )
    )
    return ts


def main() -> None:
    emitter = DatahubRestEmitter(GMS)
    # base schema so the field exists
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=DS,
            aspect=SchemaMetadataClass(
                schemaName="__backdate_test",
                platform="urn:li:dataPlatform:postgres",
                version=0,
                hash="",
                platformSchema=OtherSchemaClass(rawSchema=""),
                fields=[
                    SchemaFieldClass(
                        fieldPath=FIELD,
                        type=SchemaFieldDataTypeClass(type=StringTypeClass()),
                        nativeDataType="text",
                    )
                ],
            ),
        )
    )
    t1 = emit_desc(emitter, "Backdated description v1.", days_ago=120,
                   actor="urn:li:corpuser:agent_alpha")
    time.sleep(1)
    t2 = emit_desc(emitter, "", days_ago=40,
                   actor="urn:li:corpuser:steward_bob")  # a later clear
    print(f"emitted v1 backdated ~120d (actor agent_alpha), stamp_ms={t1}")
    print(f"emitted clear backdated ~40d (actor steward_bob), stamp_ms={t2}")

    time.sleep(3)
    txns = fetch_timeline(GMS, DS, categories=["DOCUMENTATION"])
    print(f"=== raw timeline transactions: {len(txns)} ===")
    for t in txns:
        age = (time.time() - t.get("timestamp", 0) / 1000) / 86400
        print(f"  ts={t.get('timestamp')} (~{age:.0f}d ago) actor={t.get('actor')}")
    changes = parse_timeline(txns)
    print("=== parsed changes ===")
    for c in changes:
        age = (time.time() - c.ts) / 86400
        print(f"  {c.operation:6s} {c.work_kind:10s} field={c.field} "
              f"actor={c.actor} ~{age:.0f}d ago value={c.value!r}")


if __name__ == "__main__":
    main()
