"""Thin synchronous wrapper around the DataHub MCP server.

Agents read the catalog exclusively through MCP tools, the same interface
any third-party agent uses; nothing here shortcuts to the SDK. One server
process is spawned per session and reused across calls (startup costs more
than a call does).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _server_command() -> str:
    explicit = os.environ.get("MCP_SERVER_DATAHUB")
    if explicit:
        return explicit
    found = shutil.which("mcp-server-datahub")
    if found:
        return found
    raise RuntimeError(
        "mcp-server-datahub not found: set MCP_SERVER_DATAHUB or add it to PATH"
    )


class DataHubMCP:
    """Sync facade over an MCP stdio session. Use as a context manager.

    Defaults to spawning mcp-server-datahub; pass command/args/extra_env to
    front any other stdio MCP server (e.g. the heimdall trust gateway).
    """

    def __init__(
        self,
        gms_url: Optional[str] = None,
        mutations: bool = True,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        extra_env: Optional[dict[str, str]] = None,
    ):
        self.gms_url = gms_url or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
        self.mutations = mutations
        self.command = command or _server_command()
        self.args = args or []
        self.extra_env = extra_env or {}
        self._loop = asyncio.new_event_loop()
        self._session: Optional[ClientSession] = None
        self._cm_stack: list[Any] = []

    def __enter__(self) -> "DataHubMCP":
        self._loop.run_until_complete(self._start())
        return self

    def __exit__(self, *exc: Any) -> None:
        self._loop.run_until_complete(self._stop())
        self._loop.close()

    async def _start(self) -> None:
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env={
                **os.environ,
                "DATAHUB_GMS_URL": self.gms_url,
                "TOOLS_IS_MUTATION_ENABLED": "true" if self.mutations else "false",
                **self.extra_env,
            },
        )
        stdio_cm = stdio_client(params)
        read, write = await stdio_cm.__aenter__()
        self._cm_stack.append(stdio_cm)
        session_cm = ClientSession(read, write)
        self._session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)
        await self._session.initialize()

    async def _stop(self) -> None:
        while self._cm_stack:
            cm = self._cm_stack.pop()
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    def list_tools(self) -> list[Any]:
        assert self._session is not None, "use DataHubMCP as a context manager"
        result = self._loop.run_until_complete(self._session.list_tools())
        return result.tools

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Call a tool; return parsed JSON when the response is JSON text."""
        assert self._session is not None, "use DataHubMCP as a context manager"
        result = self._loop.run_until_complete(self._session.call_tool(tool, args))
        text = "\n".join(b.text for b in result.content if getattr(b, "text", None))
        if result.isError:
            raise RuntimeError(f"MCP tool {tool} failed: {text[:500]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # convenience wrappers for the read tools agents lean on

    def search(self, query: str, num_results: int = 30) -> Any:
        return self.call("search", {"query": query, "num_results": num_results})

    def get_lineage(self, urn: str, upstream: bool, max_hops: int = 3) -> Any:
        return self.call(
            "get_lineage", {"urn": urn, "upstream": upstream, "max_hops": max_hops}
        )

    def list_schema_fields(self, urn: str) -> Any:
        return self.call("list_schema_fields", {"urn": urn})

    def get_entities(self, urns: list[str]) -> Any:
        return self.call("get_entities", {"urns": urns})
