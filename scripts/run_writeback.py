"""Project the ledger into DataHub and prove other agents can see it.

Reads the demo ledger database (populated by run_blast_demo.py and
run_agents_demo.py), then:

  1. writes accepted enrichment descriptions into the catalog for good
  2. pre-creates provenance tags and structured property definitions
  3. stamps every authored dataset with its author's tag, trust, verdict
  4. publishes one trust dossier Document per agent via save_document

Verification runs on a FRESH MCP session, because that is the point: a
different agent connecting later must find the trust artifacts. The first
dossier ever saved also flips the server's document tools on, so the fresh
session should expose search_documents where the first session did not.

Run on the box:
  ~/dh/bin/python scripts/run_writeback.py
"""

from __future__ import annotations

import json
import os

from heimdall.claims import ClaimStore
from heimdall.mcp_client import DataHubMCP
from heimdall.writeback import writeback

DB_PATH = os.environ.get("LEDGER_DB", os.path.expanduser("~/heimdall-demo.db"))
GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")


def main() -> None:
    with ClaimStore(DB_PATH) as store:
        with DataHubMCP() as mcp:
            summary = writeback(mcp, store, gms_url=GMS_URL)

        print("=== writeback summary ===")
        print(f"  descriptions applied : {summary['descriptions_applied']}")
        print(f"  tags ensured         : {len(summary['tags_ensured'])}")
        print(f"  properties defined   : {len(summary['properties_defined'])}")
        for urn, tag in summary["datasets_stamped"].items():
            name = urn.split(",")[1]
            print(f"  stamped {name:28s} -> {tag}")
        for title in summary["dossiers_published"]:
            print(f"  dossier: {title}")

        # ---- verification, as a different agent would see it ---------------
        print("\n=== verification (fresh MCP session) ===")
        with DataHubMCP() as mcp:
            tools = {
                t.name: t
                for t in mcp._loop.run_until_complete(
                    mcp._session.list_tools()
                ).tools
            }
            has_docs = "search_documents" in tools
            print(f"  document tools unlocked: {has_docs}")
            if has_docs:
                schema = tools["search_documents"].inputSchema.get("properties", {})
                query_arg = "query" if "query" in schema else next(iter(schema))
                found = mcp.call(
                    "search_documents", {query_arg: "agent trust dossier"}
                )
                text = found if isinstance(found, str) else json.dumps(found)
                hits = text.count("Agent trust dossier")
                print(f"  search_documents('agent trust dossier') mentions: {hits}")

            stamped = list(summary["datasets_stamped"])
            if stamped:
                probe = stamped[0]
                snapshot = json.dumps(mcp.get_entities([probe]))
                applied_text = next(
                    (
                        c.prediction["description"][:40]
                        for c in store.claims(settled=True)
                        if c.entity_urn == probe and c.correct and "description" in c.prediction
                    ),
                    None,
                )
                print(f"  probe dataset: {probe.split(',')[1]}")
                print(f"    provenance tag visible : {'heimdall-' in snapshot}")
                print(
                    f"    structured props visible: "
                    f"{'io.heimdall.author_agent' in snapshot or 'heimdall.author' in snapshot}"
                )
                print(
                    f"    applied description live: "
                    f"{applied_text is not None and applied_text in snapshot}"
                )


if __name__ == "__main__":
    main()
