#!/usr/bin/env python
"""A generic autonomous MCP agent. It has NO knowledge of Heimdall.

This file imports only the standard MCP client and an HTTP client for an
OpenAI-compatible model. It contains nothing from heimdall.* : no gateway, no
observability, no grounding. It connects to whatever MCP server it is pointed
at (AGENT_MCP_COMMAND / AGENT_MCP_ARGS), lists the tools, and pursues a
natural-language task by letting the model choose tool calls in a loop.

Point AGENT_MCP_COMMAND at mcp-server-datahub and it talks to DataHub directly.
Point it at `python -m heimdall.gateway` and this exact, unchanged code is
observed and graded. That one-line swap, with zero edits here, is the whole
agent-agnostic claim: Heimdall watches any MCP agent, including ones that have
never heard of it.

Env: AGENT_MCP_COMMAND, AGENT_MCP_ARGS, AGENT_TASK, AGENT_MAX_STEPS,
     LLM_BASE_URL, LLM_MODEL, OPENROUTER_API_KEY (or LLM_API_KEY).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

SYSTEM = (
    "You are a data catalog agent connected to a metadata catalog through MCP "
    "tools. Pursue the user's task by calling tools. Reply with exactly ONE "
    "JSON object per turn and nothing else:\n"
    '  {"tool": "<name>", "args": { ... }}  to call a tool, or\n'
    '  {"done": true}                         when the task is complete.\n'
    "Use the tool argument names exactly as given in the schemas. The task asks "
    "you to WRITE metadata: do not report done until you have actually called "
    "the write tools (descriptions and PII tags). If a read tool fails, do not "
    "give up: proceed to the writes the task requires."
)


def ask_model(messages: list[dict]) -> str:
    base = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "qwen/qwen3-32b")
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "temperature": 0,
              "max_tokens": 800, "reasoning": {"enabled": False}},
        timeout=120.0,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"] or ""
    return _THINK.sub("", content).strip()


def parse_action(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def tool_brief(tools) -> str:
    out = []
    for t in tools:
        props = (t.inputSchema or {}).get("properties", {})
        out.append({"name": t.name,
                    "description": (t.description or "")[:120],
                    "args": list(props.keys())})
    return json.dumps(out)[:6000]


async def run() -> None:
    command = os.environ["AGENT_MCP_COMMAND"]
    args = os.environ.get("AGENT_MCP_ARGS", "").split()
    task = os.environ.get("AGENT_TASK", "Document the dataset's columns.")
    max_steps = int(os.environ.get("AGENT_MAX_STEPS", "6"))

    params = StdioServerParameters(command=command, args=args, env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"agent: connected, {len(tools)} tools available")

            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user",
                 "content": f"Task: {task}\n\nAvailable tools:\n{tool_brief(tools)}"},
            ]

            for step in range(max_steps):
                reply = ask_model(messages)
                action = parse_action(reply)
                if action is None:
                    messages.append({"role": "user",
                                     "content": "Reply with ONLY one JSON object."})
                    continue
                if action.get("done"):
                    print(f"agent: reported done after {step} steps")
                    return
                tool, targs = action.get("tool"), action.get("args", {})
                if not tool:
                    messages.append({"role": "user",
                                     "content": 'Include a "tool" field.'})
                    continue
                print(f"agent step {step}: {tool} {json.dumps(targs)[:120]}")
                try:
                    result = await session.call_tool(tool, targs)
                    text = "\n".join(b.text for b in result.content
                                     if getattr(b, "text", None))
                    status = "error" if result.isError else "ok"
                except Exception as exc:  # keep the agent going
                    text, status = str(exc), "error"
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user",
                                 "content": f"Result ({status}): {text[:500]}"})
            print(f"agent: stopped after {max_steps} steps")


if __name__ == "__main__":
    asyncio.run(run())
