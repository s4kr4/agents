"""Live MCP protocol round trip: launches mcp_server.py as a real stdio child process.

This exercises actual stdio framing (not just the in-process FastMCP object
graph -- see D10 in the implementation plan), using the MCP Python SDK's own
stdio client so the initialize/tools-list/tools-call sequence follows the
SDK's lifecycle rather than a hand-rolled one.

The child inherits the *current* process environment (including any
LLM_MEMORY_* variables a caller has set), so callers that must not touch the
real Vault/local dirs (unit tests, ``make memory-mcp-check``) are responsible
for setting those variables before invoking ``check()``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = {
    "get_context",
    "search",
    "history",
    "write_memory",
    "forget",
    "list_unextracted",
    "mark_extracted",
}

_SERVER_PATH = Path(__file__).resolve().with_name("mcp_server.py")


async def check() -> None:
    """Round-trip initialize -> tools/list -> tools/call against a real subprocess."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(_SERVER_PATH)],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read,
            write,
            client_info=types.Implementation(name="check-mcp", version="0.0.1"),
        ) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if names != EXPECTED_TOOLS:
                raise SystemExit(f"unexpected MCP tools: {sorted(names)}")

            written = await session.call_tool(
                "write_memory",
                {"key": "editor", "summary": "Neovim", "memory_type": "profile"},
            )
            if written.isError:
                raise SystemExit(f"write_memory unexpectedly failed: {written}")

            found = await session.call_tool("search", {"query": "Neovim"})
            if found.isError or found.structuredContent is None:
                raise SystemExit(f"search unexpectedly failed: {found}")
            if not found.structuredContent.get("count"):
                raise SystemExit(f"search did not find the memory just written: {found}")

            # write_memory's automatic session should be labeled with the
            # connecting client's declared name (see mcp_server.py's
            # client_label / P-4), not the bare "mcp" fallback used
            # in-process without a live session.
            history = await session.call_tool("history", {})
            if history.isError or history.structuredContent is None:
                raise SystemExit(f"history unexpectedly failed: {history}")
            sessions = history.structuredContent.get("sessions", [])
            if not sessions or sessions[0].get("client") != "mcp:check-mcp":
                raise SystemExit(
                    f"automatic session was not labeled with the client name: {history}"
                )

            invalid = await session.call_tool(
                "write_memory",
                {
                    "key": "api",
                    "summary": "REST",
                    "memory_type": "reference",
                    "scope": "project",
                },
            )
            if not invalid.isError:
                raise SystemExit("write_memory with scope=project and no project_id should fail")

            still_alive = await session.call_tool("search", {})
            if still_alive.isError:
                raise SystemExit(f"server did not stay usable after a tool error: {still_alive}")


if __name__ == "__main__":
    asyncio.run(check())
