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
    "related",
    "list_tags",
    "update_metadata",
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

            # Tags/related round trip: tagged write_memory -> search(tags=...)
            # to recover the id -> update_metadata to replace, then clear ->
            # list_tags. Also verifies update_metadata does not touch the
            # memory's title/summary/updated (a pure metadata edit, see
            # mcp_server.py's update_metadata docstring).
            tagged = await session.call_tool(
                "write_memory",
                {
                    "key": "check-tags",
                    "summary": "Tag round-trip check",
                    "memory_type": "reference",
                    "tags": ["docker", "GPU"],
                },
            )
            if tagged.isError:
                raise SystemExit(f"write_memory with tags unexpectedly failed: {tagged}")

            tag_search = await session.call_tool(
                "search", {"query": "Tag round-trip", "tags": ["docker"]}
            )
            if tag_search.isError or tag_search.structuredContent is None:
                raise SystemExit(f"search with tags unexpectedly failed: {tag_search}")
            memories = tag_search.structuredContent.get("memories", [])
            if not memories:
                raise SystemExit(f"search(tags=...) did not find the tagged memory: {tag_search}")
            before = memories[0]

            updated = await session.call_tool(
                "update_metadata",
                {"memory_id": before["id"], "tags": ["docker", "gpu", "linux"]},
            )
            if updated.isError or updated.structuredContent is None:
                raise SystemExit(f"update_metadata unexpectedly failed: {updated}")
            after = updated.structuredContent["memory"]
            for field in ("id", "type", "title", "summary", "updated"):
                if before[field] != after[field]:
                    raise SystemExit(
                        f"update_metadata must not change {field}: before={before} after={after}"
                    )
            if sorted(after["tags"]) != ["docker", "gpu", "linux"]:
                raise SystemExit(f"update_metadata did not replace tags as expected: {updated}")

            cleared = await session.call_tool(
                "update_metadata", {"memory_id": before["id"], "tags": []}
            )
            if cleared.isError or cleared.structuredContent is None:
                raise SystemExit(f"update_metadata clearing tags unexpectedly failed: {cleared}")
            if cleared.structuredContent["memory"]["tags"] != []:
                raise SystemExit(f"update_metadata did not clear tags: {cleared}")

            related = await session.call_tool("related", {"memory_id": before["id"]})
            if related.isError:
                raise SystemExit(f"related unexpectedly failed: {related}")

            tags_listing = await session.call_tool("list_tags", {})
            if tags_listing.isError:
                raise SystemExit(f"list_tags unexpectedly failed: {tags_listing}")


if __name__ == "__main__":
    asyncio.run(check())
