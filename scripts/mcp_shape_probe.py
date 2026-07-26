"""Throwaway probe: what does the MCP server actually show, and what survives a delta.

Two facts decide the shape of the evolution engine and of "an already documented
column is done", and neither can be guessed from our side of the wire:

  1. Does `list_schema_fields` expose a column's TAGS as well as its description?
    An agent has to find its own remaining work through MCP, so a PII tagger can
    only skip an already-tagged column if the tag is visible there.

  2. Does re-emitting `schemaMetadata` (which is how a schema change is applied as
    a delta, without re-ingesting the catalog) wipe what agents have written?
    DataHub keeps agent edits in the editable overlay, so it should not, but a
    mutation engine that silently erases every description each day would look
    like it worked while destroying the evidence.

Prints raw shapes rather than asserting, then hard-deletes what it made.

Run on the box:
    set -a; . ~/.heimdall/env; set +a
    ~/fresh-e2e/v/bin/python scripts/mcp_shape_probe.py
"""

from __future__ import annotations

import json
import os
import sys


def brief(obj, limit: int = 1200) -> str:
    return json.dumps(obj, indent=2, default=str)[:limit]


def main() -> int:
    if not os.environ.get("MCP_SERVER_DATAHUB"):
        print("FATAL: MCP_SERVER_DATAHUB required", file=sys.stderr)
        return 2

    from heimdall.catalog import CatalogSpec, ColumnSpec, DatasetSpec, spec_to_world
    from heimdall.ingest import build_mcps, hard_delete_catalog, ingest_spec
    from heimdall.mcp_client import DataHubMCP

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    spec = CatalogSpec(
        catalog="hcatalog_probe01",
        theme="probe",
        datasets=[DatasetSpec(
            name="raw_probe",
            owner="probe-team",
            columns=[
                ColumnSpec(name="buyer_email", pii="email"),
                ColumnSpec(name="net_total_usd", gold_keywords=["total", "usd"]),
                ColumnSpec(name="doomed_column"),
            ],
        )],
    )
    urn = spec_to_world(spec).datasets["raw_probe"].urn
    print(f"urn: {urn}")

    try:
        n = ingest_spec(spec, gms_url=gms)
        print(f"ingested {n} mcps")

        with DataHubMCP(gms_url=gms) as mcp:
            print("\n=== 1. tools available ===")
            print(", ".join(sorted(t.name for t in mcp.list_tools())))

            print("\n=== 2. schema fields, virgin ===")
            print(brief(mcp.list_schema_fields(urn)))

            print("\n=== 3. an agent describes and tags ===")
            mcp.call("update_description", {
                "entity_urn": urn, "column_path": "net_total_usd",
                "description": "AGENT WROTE THIS: net sale total in usd.",
                "operation": "replace",
            })
            mcp.call("add_tags", {
                "entity_urns": [urn], "column_paths": ["buyer_email"],
                "tag_urns": ["urn:li:tag:pii-email"],
            })
            print("wrote a description and a tag")

            print("\n=== 4. schema fields, after agent writes ===")
            after = mcp.list_schema_fields(urn)
            print(brief(after, 2000))
            fields = after.get("fields", []) if isinstance(after, dict) else []
            print("\nper-field keys seen:", sorted({k for f in fields for k in f}))

            print("\n=== 5. get_entities, to see where tags actually live ===")
            print(brief(mcp.get_entities([urn]), 2500))

        print("\n=== 6. re-emit schemaMetadata as a delta: drop a column, add a column ===")
        spec.datasets[0].columns = [
            ColumnSpec(name="buyer_email", pii="email"),
            ColumnSpec(name="net_total_usd", gold_keywords=["total", "usd"]),
            ColumnSpec(name="brand_new_column"),  # doomed_column removed
        ]
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        emitter = DatahubRestEmitter(gms)
        for mcp_obj in build_mcps(spec):
            emitter.emit(mcp_obj)
        print("re-emitted")

        with DataHubMCP(gms_url=gms) as mcp:
            print("\n=== 7. schema fields after the delta ===")
            post = mcp.list_schema_fields(urn)
            print(brief(post, 2000))
            names = [f.get("fieldPath") for f in (post.get("fields", []) or [])]
            print("\ncolumns now:", names)
            print("dropped column gone:", "doomed_column" not in names)
            print("new column present:", "brand_new_column" in names)
            kept = [f for f in (post.get("fields", []) or [])
                    if f.get("fieldPath") == "net_total_usd"]
            print("agent description survived the delta:",
                  bool(kept) and "AGENT WROTE THIS" in json.dumps(kept))
            tagged = [f for f in (post.get("fields", []) or [])
                      if f.get("fieldPath") == "buyer_email"]
            print("agent tag survived the delta:", "pii-email" in json.dumps(tagged))

    finally:
        print("\n=== cleanup ===")
        gone = sum(1 for r in hard_delete_catalog(spec, gms_url=gms) if r.ok)
        print(f"hard-deleted {gone} datasets")

    return 0


if __name__ == "__main__":
    sys.exit(main())
