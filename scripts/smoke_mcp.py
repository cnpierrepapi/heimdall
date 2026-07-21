"""End-to-end smoke test for the heimdall stack.

Verifies, against a live DataHub instance:
  1. the DataHub MCP server starts and lists its tools
  2. read path: search for a dataset, then fetch its lineage
  3. write path: apply a tag via MCP mutation tools
  4. ledger path: record one claim in SQLite and read it back

Run on the box: ~/dh/bin/python smoke_mcp.py
"""

import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
DB_PATH = os.path.expanduser("~/heimdall-smoke.db")


def content_text(result):
    parts = []
    for block in result.content:
        if getattr(block, "text", None):
            parts.append(block.text)
    return "\n".join(parts)


def ensure_tag_exists():
    """Tags are entities in DataHub; create ours before applying it.

    Also exercises the SDK write path the writeback layer relies on.
    """
    from datahub.emitter.mce_builder import make_tag_urn
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import TagPropertiesClass

    emitter = DatahubRestEmitter(GMS_URL)
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=make_tag_urn("heimdall-smoke"),
            aspect=TagPropertiesClass(
                name="heimdall-smoke",
                description="Smoke test tag created by heimdall setup verification.",
            ),
        )
    )
    print("[0] tag urn:li:tag:heimdall-smoke ensured via SDK")


async def main():
    ensure_tag_exists()
    server = StdioServerParameters(
        command=os.path.expanduser("~/dh/bin/mcp-server-datahub"),
        env={
            **os.environ,
            "DATAHUB_GMS_URL": GMS_URL,
            "TOOLS_IS_MUTATION_ENABLED": "true",
        },
    )
    report = {}
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            report["tools"] = names
            print(f"[1] tools ({len(names)}): {', '.join(names)}")

            schemas = {t.name: t.inputSchema for t in tools.tools}

            search_res = await session.call_tool(
                "search", {"query": "*", "num_results": 30}
            )
            search_text = content_text(search_res)
            report["search_ok"] = not search_res.isError
            print(f"[2] search isError={search_res.isError}")

            # pull the first dataset urn out of the search response
            urn = None
            try:
                payload = json.loads(search_text)
                for hit in payload.get("searchResults", []):
                    hit_urn = hit.get("entity", {}).get("urn", "")
                    if hit_urn.startswith("urn:li:dataset:"):
                        urn = hit_urn
                        break
            except json.JSONDecodeError:
                for token in search_text.replace('"', " ").replace(",", " ").split():
                    if token.startswith("urn:li:dataset:"):
                        urn = token
                        break
            report["dataset_urn"] = urn
            print(f"    first dataset urn: {urn}")
            if urn is None:
                print("    NO URN FOUND, dumping search response:")
                print(search_text[:2000])
                sys.exit(1)

            lineage_res = await session.call_tool(
                "get_lineage",
                {"urn": urn, "upstream": False, "max_hops": 2},
            )
            report["lineage_ok"] = not lineage_res.isError
            print(f"[3] get_lineage isError={lineage_res.isError}")
            if lineage_res.isError:
                print("    lineage schema was:", json.dumps(schemas.get("get_lineage", {})))
                print("    error:", content_text(lineage_res)[:1000])

            tag_res = await session.call_tool(
                "add_tags",
                {"entity_urns": [urn], "tag_urns": ["urn:li:tag:heimdall-smoke"]},
            )
            report["add_tags_ok"] = not tag_res.isError
            print(f"[4] add_tags isError={tag_res.isError}")
            if tag_res.isError:
                print("    add_tags schema was:", json.dumps(schemas.get("add_tags", {})))
                print("    error:", content_text(tag_res)[:1000])

    # ledger path: one claim in, one claim out
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS claims (
            claim_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            entity_urn TEXT NOT NULL,
            prediction TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_ts REAL NOT NULL,
            settled_ts REAL,
            outcome TEXT
        )"""
    )
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO claims (claim_id, agent_id, claim_type, entity_urn, prediction, confidence, created_ts) VALUES (?,?,?,?,?,?,?)",
        (cid, "smoke-agent", "smoke", urn, json.dumps({"tagged": True}), 0.99, time.time()),
    )
    conn.commit()
    row = conn.execute("SELECT agent_id, entity_urn FROM claims WHERE claim_id=?", (cid,)).fetchone()
    conn.close()
    report["claim_ok"] = row is not None
    print(f"[5] claim recorded and read back: {row}")

    ok = all(report.get(k) for k in ("search_ok", "lineage_ok", "add_tags_ok", "claim_ok"))
    print("SMOKE " + ("PASS" if ok else "PARTIAL") + " " + json.dumps({k: v for k, v in report.items() if k != "tools"}))


if __name__ == "__main__":
    asyncio.run(main())
