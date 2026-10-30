"""Minimal MCP stdio client for Polytone's own servers.

The PRD's generation step "executes an execute_read MCP tool call to retrieve the top-k
structural constraints". ``get_constraints_via_mcp(persona, k)`` does exactly that: it spawns
``python -m backend.mcp_servers.graph_server`` over stdio, calls the ``get_constraints`` tool and
returns the same ``Constraints`` dict that ``GraphStore.get_constraints`` returns in-process.

``/generate`` keeps calling the in-process store (spawning a process + MCP handshake costs
~0.5-1s against a 5s budget); both paths run the identical ``store.get_constraints`` code, so
results are equivalent (see ``backend/tests/test_mcp_graph.py::test_mcp_stdio_end_to_end``).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from backend.config import REPO_ROOT


def server_params(module: str = "backend.mcp_servers.graph_server", env: dict[str, str] | None = None) -> Any:
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        env={**{k: v for k, v in os.environ.items() if k.startswith(("POLYTONE_", "NEO4J_", "PATH"))}, **(env or {})},
        cwd=str(REPO_ROOT),
    )


def _result_payload(result: Any) -> Any:
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        texts = [getattr(c, "text", "") for c in getattr(result, "content", [])]
        raise RuntimeError("MCP tool error: " + " ".join(t for t in texts if t))
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    for c in getattr(result, "content", []) or []:
        text = getattr(c, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


async def call_tool_async(
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    module: str = "backend.mcp_servers.graph_server",
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Any:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async def _run() -> Any:
        async with stdio_client(server_params(module, env)) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return _result_payload(await session.call_tool(tool, arguments or {}))

    return await asyncio.wait_for(_run(), timeout)


def call_tool(tool: str, arguments: dict[str, Any] | None = None, **kw: Any) -> Any:
    """Synchronous wrapper (spawns the server, calls one tool, exits)."""
    return asyncio.run(call_tool_async(tool, arguments, **kw))


async def list_tools_async(module: str = "backend.mcp_servers.graph_server", env: dict[str, str] | None = None) -> list[str]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(server_params(module, env)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.list_tools()
        return [t.name for t in res.tools]


def get_constraints_via_mcp(persona: str | None = None, k: int = 20, **kw: Any) -> dict[str, Any]:
    return call_tool("get_constraints", {"persona": persona or "Default", "k": k}, **kw)


def execute_read_via_mcp(query: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return call_tool("execute_read", {"query": query, "params": params or {}}, **kw)
