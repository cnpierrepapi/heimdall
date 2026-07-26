"""Probe 2: can an agent SEE the description the catalog shipped with?

Probe 1 answered the tag question but left the one that decides whether the
scoring rule is fair. Every column it ingested was undocumented, so the only
description that ever appeared was `editedDescription`, written by an agent. If
`list_schema_fields` never exposes the description carried in `schemaMetadata`,
then an agent cannot tell a column the catalog already documented from a blank
one. It would do the work, earn no score, and have no way to know why, which
breaks the rule that an agent is only judged on evidence it can observe.

Also checks whether clearing the editable description actually hands a column
back as new work, which is what doc rot has to do.

Run on the box:
    set -a; . ~/.heimdall/env; set +a
    ~/fresh-e2e/v/bin/python scripts/mcp_shape_probe2.py
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    from heimdall.catalog import CatalogSpec, ColumnSpec, DatasetSpec, spec_to_world
    from heimdall.ingest import hard_delete_catalog, ingest_spec
    from heimdall.mcp_client import DataHubMCP

    gms = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    spec = CatalogSpec(
        catalog="hcatalog_probe02",
        theme="probe",
        datasets=[DatasetSpec(
            name="raw_probe2",
            owner="probe-team",
            columns=[
                ColumnSpec(name="shipped_documented",
                           description="CATALOG SHIPPED THIS description."),
                ColumnSpec(name="shipped_blank", description=None),
            ],
        )],
    )
    urn = spec_to_world(spec).datasets["raw_probe2"].urn

    try:
        ingest_spec(spec, gms_url=gms)
        with DataHubMCP(gms_url=gms) as mcp:
            fields = mcp.list_schema_fields(urn).get("fields", [])
            print("MARK fields:", json.dumps(fields))
            doc = next(f for f in fields if f["fieldPath"] == "shipped_documented")
            print("MARK shipped description visible via MCP:",
                  "CATALOG SHIPPED THIS" in json.dumps(doc))
            print("MARK keys on the documented column:", sorted(doc))

            ents = mcp.get_entities([urn])
            print("MARK shipped description visible in get_entities:",
                  "CATALOG SHIPPED THIS" in json.dumps(ents, default=str))

            # does clearing the editable description hand a column back as new work
            mcp.call("update_description", {
                "entity_urn": urn, "column_path": "shipped_blank",
                "description": "agent text here.", "operation": "replace"})
            after = mcp.list_schema_fields(urn).get("fields", [])
            blank = next(f for f in after if f["fieldPath"] == "shipped_blank")
            print("MARK after agent write:", json.dumps(blank))
            mcp.call("update_description", {
                "entity_urn": urn, "column_path": "shipped_blank",
                "operation": "remove"})
            cleared = mcp.list_schema_fields(urn).get("fields", [])
            blank2 = next(f for f in cleared if f["fieldPath"] == "shipped_blank")
            print("MARK after remove:", json.dumps(blank2))
            print("MARK removal hands the column back:",
                  not str(blank2.get("editedDescription") or "").strip())
    finally:
        gone = sum(1 for r in hard_delete_catalog(spec, gms_url=gms) if r.ok)
        print(f"MARK cleanup deleted {gone}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
