"""Shared service results, validation and extraction provenance."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import memory as mem
from local_store import LocalPipelineStore
from markdown_store import MarkdownMemoryStore


class TestMemoryService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.local = LocalPipelineStore(root / "local")
        self.vault = MarkdownMemoryStore(root / "vault")

    def args(self, **changes):
        fields = dict(
            db=None,
            markdown_store=self.vault,
            local_store=self.local,
            project_id=None,
            session_id="source",
            user_id="default",
            client="test",
            query=None,
            memory_type="profile",
            scope="global",
            entity_id="default",
            entity_type="user",
            key="editor",
            summary="Neovim",
            confidence=0.8,
            limit=10,
            include_memories=True,
            include_sessions=True,
            include_events=True,
            role=None,
            kind=None,
            memory_id="global/missing",
            reason="test",
        )
        fields.update(changes)
        return argparse.Namespace(**fields)

    def run_service(self, name, args):
        self.assertTrue(hasattr(mem, name), name)
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = getattr(mem, name)(args)
        self.assertEqual(stream.getvalue(), "")
        self.assertIsInstance(result, dict)
        return result

    def test_nine_operations_return_dicts_without_stdout(self):
        self.run_service("run_init_db", self.args())
        self.run_service("run_start_session", self.args())
        write = self.run_service("run_write_memory", self.args())
        self.assertTrue(write["ok"])
        for name in (
            "run_get_context",
            "run_search",
            "run_history",
            "run_list_unextracted",
            "run_mark_extracted",
            "run_forget",
        ):
            with self.subTest(name=name):
                self.assertTrue(self.run_service(name, self.args())["ok"])

    def test_invalid_write_leaves_no_events_observations_or_memories(self):
        self.assertTrue(hasattr(mem, "MemoryUsageError"))
        for changes in (
            {"confidence": float("nan")},
            {"confidence": float("inf")},
            {"confidence": -0.1},
            {"confidence": 1.1},
            {"confidence": True},
            {"key": ""},
            {"summary": " "},
            {"scope": "client"},
            {"memory_type": "invalid"},
            {"scope": "project"},
            {"session_id": "../outside"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(mem.MemoryUsageError):
                    self.run_service("run_write_memory", self.args(**changes))
                self.assertEqual(self.local.iter_all_events(), [])
                self.assertEqual(self.local.iter_all_observations(), [])
                self.assertEqual(self.vault.iter_all(), [])

    def test_explicit_session_inherits_project_and_preserves_source(self):
        self.local.ensure_session("source", client="codex", user_id="default", project_id="alpha")
        self.run_service(
            "run_write_memory",
            self.args(
                scope="project",
                source="mcp_extract",
                extractor_version="mcp-v1",
                require_session=True,
            ),
        )
        observation = self.local.iter_observations("source")[0]
        self.assertEqual(observation["project_id"], "alpha")
        self.assertEqual(observation["value"]["source"], "mcp_extract")
        self.assertEqual(observation["extractor_version"], "mcp-v1")
        self.assertEqual(self.vault.search(scope="project")[0]["project_id"], "alpha")
        result = self.run_service("run_history", self.args(project_id="alpha"))
        self.assertEqual(len(result["events"]), 1)

    def test_mismatched_or_missing_explicit_session_has_no_write_side_effects(self):
        self.assertTrue(hasattr(mem, "MemoryUsageError"))
        self.local.ensure_session("source", client="codex", user_id="default", project_id=None)
        for changes in ({"project_id": "alpha"}, {"session_id": "missing"}):
            with self.subTest(changes=changes):
                with self.assertRaises(mem.MemoryUsageError):
                    self.run_service("run_write_memory", self.args(require_session=True, **changes))
                self.assertEqual(self.local.iter_all_events(), [])
                self.assertEqual(self.local.iter_all_observations(), [])
                self.assertEqual(len(self.local.list_sessions()), 1)


if __name__ == "__main__":
    unittest.main()
