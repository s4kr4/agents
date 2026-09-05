"""MCP schema, session isolation and live stdio protocol integration."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError


class _IsolatedMemoryEnvironment(unittest.IsolatedAsyncioTestCase):
    """Common vault/local/config isolation so no test touches real storage.

    Every subclass (in-process tool calls and the real stdio subprocess
    round trip alike) must never resolve to the user's actual Vault/local
    dirs or config search path -- see M-1 in the safety review.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {
            "LLM_MEMORY_VAULT": str(self.root / "vault"),
            "LLM_MEMORY_LOCAL_DIR": str(self.root / "local"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "LLM_MEMORY_QUEUE_DIR": str(self.root / "queue"),
        }
        self.env_patch = patch.dict(os.environ, env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # LLM_MEMORY_CONFIG, if set in the calling shell, would otherwise
        # override the isolated XDG_CONFIG_HOME above and point back at a
        # real config file; patch.dict's own restore (above) puts it back.
        os.environ.pop("LLM_MEMORY_CONFIG", None)


class TestMcpServer(_IsolatedMemoryEnvironment):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertIsNotNone(importlib.util.find_spec("mcp_server"))
        from mcp_server import create_server

        self.server = create_server()

    async def call(self, name, **arguments):
        result = await self.server.call_tool(name, arguments)
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple):
            return result[1]
        return json.loads(result[0].text)

    async def test_seven_tools_have_expected_schema(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "get_context",
                "search",
                "history",
                "write_memory",
                "forget",
                "list_unextracted",
                "mark_extracted",
            },
        )
        self.assertNotIn("session_id", tools["write_memory"].inputSchema["required"])
        for name in ("search", "history", "list_unextracted"):
            self.assertEqual(tools[name].inputSchema["properties"]["limit"]["maximum"], 100)
            self.assertEqual(tools[name].inputSchema["properties"]["limit"]["minimum"], 1)

    async def test_automatic_sessions_are_reused_per_project_and_not_unextracted(self):
        for project in ("alpha", "alpha", "beta"):
            await self.call(
                "write_memory",
                key="editor",
                summary=f"Neovim {project}",
                memory_type="profile",
                project_id=project,
            )
        result = await self.call("history")
        self.assertEqual(len(result["sessions"]), 2)
        self.assertEqual(len((await self.call("history", project_id="alpha"))["events"]), 2)
        self.assertEqual(len((await self.call("history", project_id="beta"))["events"]), 1)
        self.assertEqual((await self.call("list_unextracted"))["count"], 0)

    async def test_automatic_session_writes_are_labeled_mcp_not_extract(self):
        """An automatic (session_id omitted) write is a fresh MCP-originated
        observation, not part of a memory-extract pass over an existing
        conversation -- only an explicit session_id means the latter."""
        from local_store import LocalPipelineStore

        await self.call("write_memory", key="editor", summary="Neovim", memory_type="profile")
        local = LocalPipelineStore(self.root / "local")
        sessions = local.list_sessions()
        self.assertEqual(len(sessions), 1)
        observation = local.iter_observations(sessions[0]["id"])[0]
        self.assertEqual(observation["value"]["source"], "mcp")

    async def test_project_scoped_write_without_project_id_is_reachable_by_history(self):
        """scope="project" with entity_type="project" derives project_id from
        entity_id (see memory.resolve_effective_project_id). The automatic
        session must be tagged with that same effective project_id so
        history(project_id=...) can find it -- not left with project_id=None."""
        await self.call(
            "write_memory",
            key="ci-pipeline",
            summary="lint -> test -> build",
            memory_type="reference",
            scope="project",
            entity_type="project",
            entity_id="alpha",
        )
        result = await self.call("history", project_id="alpha")
        self.assertGreaterEqual(result["counts"]["sessions"], 1)
        self.assertGreaterEqual(result["counts"]["events"], 1)

    async def test_concurrent_writes_for_same_project_create_one_session(self):
        """Real, concurrently-running OS threads race the check-then-create
        of the automatic per-project session; the lock in write_memory must
        collapse them onto a single session (see P-5).

        This reaches past the public ``call_tool()`` on purpose: that method
        is async and, per SDK 1.29.1, calls sync tool functions directly on
        the event loop thread with no internal yield point, so
        asyncio.gather() over several call_tool() calls would never actually
        interleave and could not exercise this race. Getting the raw
        function to call from real OS threads instead requires FastMCP's
        private ``_tool_manager`` -- there is no public accessor for it (see
        ``dir(FastMCP)``); if the SDK grows one, prefer it here.
        """
        write_memory = self.server._tool_manager.get_tool("write_memory").fn
        worker_count = 8
        barrier = threading.Barrier(worker_count)

        def call_once(index: int) -> None:
            barrier.wait()
            write_memory(
                key=f"pref-{index}",
                summary=f"value-{index}",
                memory_type="profile",
                project_id="alpha",
            )

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            list(pool.map(call_once, range(worker_count)))

        result = await self.call("history", project_id="alpha")
        self.assertEqual(result["counts"]["sessions"], 1)

    async def test_invalid_requests_leave_no_sessions_and_server_remains_usable(self):
        for changes in (
            {"confidence": float("nan")},
            {"confidence": 2},
            {"confidence": True},
            {"key": ""},
            {"summary": 4},
            {"scope": "project"},
            {"session_id": "../outside"},
        ):
            args = dict(key="editor", summary="Neovim", memory_type="profile")
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ToolError):
                await self.call("write_memory", **args)
        self.assertEqual((await self.call("history"))["sessions"], [])
        self.assertEqual((await self.call("search"))["count"], 0)
        for limit in (0, 101, True, "10"):
            with self.subTest(limit=limit), self.assertRaises(ToolError):
                await self.call("search", limit=limit)

    async def test_explicit_session_keeps_project_and_does_not_create_another(self):
        from local_store import LocalPipelineStore

        local = LocalPipelineStore(self.root / "local")
        local.ensure_session("original", client="codex", user_id="default", project_id="alpha")
        local.update_session("original", summary="source conversation")
        await self.call(
            "write_memory",
            key="api",
            summary="REST",
            memory_type="reference",
            scope="project",
            session_id="original",
        )
        self.assertEqual(len(local.list_sessions()), 1)
        obs = local.iter_observations("original")[0]
        self.assertEqual(obs["project_id"], "alpha")
        self.assertEqual(obs["value"]["source"], "mcp_extract")
        self.assertEqual((await self.call("mark_extracted", session_id="original"))["updated"], 1)
        self.assertEqual((await self.call("list_unextracted"))["count"], 0)
        with self.assertRaises(ToolError):
            await self.call(
                "write_memory",
                key="api",
                summary="REST",
                memory_type="reference",
                project_id="beta",
                session_id="original",
            )

    async def test_configuration_is_fixed_for_the_process(self):
        os.environ["LLM_MEMORY_VAULT"] = str(self.root / "changed")
        await self.call("write_memory", key="editor", summary="Neovim", memory_type="profile")
        self.assertFalse((self.root / "changed").exists())
        self.assertEqual((await self.call("search"))["count"], 1)


class TestMcpProtocol(_IsolatedMemoryEnvironment):
    async def test_live_stdio_round_trip_and_tool_errors(self):
        """Exercises the real stdio subprocess (see check_mcp.py), isolated
        the same way as TestMcpServer above so it neither reads nor writes
        the real Vault/local/config."""
        self.assertIsNotNone(importlib.util.find_spec("check_mcp"))
        from check_mcp import check

        await asyncio.wait_for(check(), timeout=40)


if __name__ == "__main__":
    unittest.main()
