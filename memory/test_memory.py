#!/usr/bin/env python3
"""Tests for the memory.py CLI (file-based Vault + local pipeline stores)."""
from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_store import LocalPipelineStore
from markdown_store import MarkdownMemoryStore

import memory as mem


class CliTestBase(unittest.TestCase):
    """Base class wiring a fresh vault/local-dir pair per test."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmpdir.name) / "vault"
        self.local_dir = Path(self._tmpdir.name) / "local"
        self.markdown_store = MarkdownMemoryStore(self.vault_dir)
        self.local_store = LocalPipelineStore(self.local_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def make_args(self, **fields) -> argparse.Namespace:
        defaults = {
            "db": None,
            "markdown_store": self.markdown_store,
            "local_store": self.local_store,
        }
        defaults.update(fields)
        return argparse.Namespace(**defaults)

    def run_cmd(self, func, **fields) -> dict:
        args = self.make_args(**fields)
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            func(args)
        return json.loads(captured.getvalue())


class TestCmdInitDb(CliTestBase):
    def test_returns_ok_and_directory_paths(self):
        result = self.run_cmd(mem.cmd_init_db)
        self.assertTrue(result["ok"])
        self.assertEqual(result["vault"], str(self.vault_dir))
        self.assertEqual(result["local_dir"], str(self.local_dir))

    def test_creates_vault_and_local_directories(self):
        self.run_cmd(mem.cmd_init_db)
        self.assertTrue((self.vault_dir / "memory").is_dir())
        self.assertTrue((self.local_dir / "sessions").is_dir())


class TestCmdStartSession(CliTestBase):
    def test_creates_session_file_and_returns_ok(self):
        result = self.run_cmd(
            mem.cmd_start_session,
            session_id="sess_1",
            client="claude-code",
            user_id="default",
            project_id="proj",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["session"]["id"], "sess_1")
        self.assertIsNotNone(self.local_store.get_session("sess_1"))

    def test_generates_session_id_when_not_given(self):
        result = self.run_cmd(
            mem.cmd_start_session, session_id=None, client="c", user_id="u", project_id=None
        )
        self.assertTrue(result["session"]["id"].startswith("sess_"))


class TestCmdAppendEvent(CliTestBase):
    def test_appends_event_to_local_store(self):
        result = self.run_cmd(
            mem.cmd_append_event,
            event_id=None,
            session_id="sess_1",
            client="claude-code",
            user_id="default",
            project_id="proj",
            role="user",
            kind="message",
            content="hello",
            importance=0.5,
        )
        self.assertTrue(result["ok"])
        events = self.local_store.iter_events("sess_1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["content"], "hello")

    def test_ensures_session_exists(self):
        self.run_cmd(
            mem.cmd_append_event,
            event_id=None,
            session_id="sess_new",
            client="claude-code",
            user_id="default",
            project_id="proj",
            role="user",
            kind="message",
            content="hi",
            importance=0.5,
        )
        self.assertIsNotNone(self.local_store.get_session("sess_new"))


class TestCmdEndSession(CliTestBase):
    def test_raises_when_session_missing(self):
        args = self.make_args(
            session_id="sess_missing",
            summary=None,
            append_summary_event=False,
            extract=False,
            consolidate=False,
        )
        with self.assertRaises(SystemExit):
            mem.cmd_end_session(args)

    def test_extract_and_consolidate_creates_active_memory(self):
        self.local_store.ensure_session("sess_1", client="claude-code", user_id="u1", project_id="p1")
        self.local_store.append_event(
            "sess_1", role="user", kind="message", content="I like Neovim", importance=0.5
        )

        result = self.run_cmd(
            mem.cmd_end_session,
            session_id="sess_1",
            summary=None,
            append_summary_event=False,
            extract=True,
            consolidate=True,
        )

        self.assertTrue(result["ok"])
        self.assertGreater(result["extracted_count"], 0)
        self.assertGreater(result["consolidated_count"], 0)

        active = self.markdown_store.search(type="profile")
        titles = [m["title"] for m in active]
        self.assertIn("Preferred Editor", titles)

    def test_append_summary_event_adds_summary_event(self):
        self.local_store.ensure_session("sess_1", client="claude-code", user_id="u1", project_id="p1")

        self.run_cmd(
            mem.cmd_end_session,
            session_id="sess_1",
            summary="custom summary",
            append_summary_event=True,
            extract=False,
            consolidate=False,
        )

        events = self.local_store.iter_events("sess_1")
        summary_events = [e for e in events if e["kind"] == "summary"]
        self.assertEqual(len(summary_events), 1)
        self.assertEqual(summary_events[0]["content"], "custom summary")

    def test_consolidate_only_processes_current_session_observations(self):
        self.local_store.ensure_session("sess_old", client="c", user_id="u1", project_id="p1")
        self.local_store.append_observation(
            session_id="sess_old",
            source_event_id="evt_old",
            entity_type="user",
            entity_id="u1",
            attribute="preferred_language_runtime",
            value={"type": "profile", "value": "TypeScript"},
            confidence=0.75,
            scope="global",
            extractor_version="test",
        )

        self.local_store.ensure_session("sess_new", client="c", user_id="u1", project_id="p1")
        self.local_store.append_observation(
            session_id="sess_new",
            source_event_id="evt_new",
            entity_type="user",
            entity_id="u1",
            attribute="preferred_language_runtime",
            value={"type": "profile", "value": "Python"},
            confidence=0.75,
            scope="global",
            extractor_version="test",
        )

        self.run_cmd(
            mem.cmd_end_session,
            session_id="sess_new",
            summary="s",
            append_summary_event=False,
            extract=False,
            consolidate=True,
        )

        active = self.markdown_store.search(type="profile")
        matching = [m for m in active if m["title"] == "Preferred Language Runtime"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["summary"], "よく使う言語: Python")

    def test_consolidate_skips_poisoned_project_scope_observation_and_processes_the_rest(self):
        self.local_store.ensure_session("sess_1", client="c", user_id="u1", project_id="p1")
        self.local_store.append_observation(
            session_id="sess_1",
            source_event_id="evt_poison",
            entity_type="user",
            entity_id="u1",
            attribute="api_routing",
            value={"type": "reference", "value": "REST"},
            confidence=0.8,
            scope="project",
            project_id=None,
            extractor_version="test",
        )
        self.local_store.append_observation(
            session_id="sess_1",
            source_event_id="evt_ok",
            entity_type="user",
            entity_id="u1",
            attribute="preferred_editor",
            value={"type": "profile", "value": "Neovim"},
            confidence=0.7,
            scope="global",
            extractor_version="test",
        )

        result = self.run_cmd(
            mem.cmd_end_session,
            session_id="sess_1",
            summary="s",
            append_summary_event=False,
            extract=False,
            consolidate=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["consolidated_count"], 1)
        active = self.markdown_store.search(type="profile")
        titles = [m["title"] for m in active]
        self.assertIn("Preferred Editor", titles)


class TestCmdExtract(CliTestBase):
    def test_inserts_observations_from_events(self):
        self.local_store.ensure_session("sess_1", client="c", user_id="u1", project_id=None)
        self.local_store.append_event(
            "sess_1", role="user", kind="message", content="I like Python", importance=0.5
        )

        result = self.run_cmd(mem.cmd_extract, session_id="sess_1")

        self.assertTrue(result["ok"])
        self.assertGreater(result["count"], 0)
        self.assertEqual(len(self.local_store.iter_observations("sess_1")), result["count"])


class TestBuildCandidatesNoRecentSummary(unittest.TestCase):
    """build_candidates() should not generate recent_summary observations from summary events."""

    def _make_event(self, **overrides) -> dict:
        defaults = {
            "id": "evt_test",
            "role": "assistant",
            "kind": "summary",
            "content": "This is a session summary text.",
            "user_id": "user_test",
            "project_id": "proj_test",
            "importance": 0.9,
            "client": "claude-code",
        }
        defaults.update(overrides)
        return defaults

    def test_summary_event_does_not_generate_recent_summary_observation(self):
        event = self._make_event(kind="summary", content="Some session summary.")
        candidates = mem.build_candidates(event)
        attributes = [c.attribute for c in candidates]
        self.assertNotIn("recent_summary", attributes)

    def test_summary_event_generates_no_candidates_at_all(self):
        event = self._make_event(kind="summary", role="assistant", content="Summary text.")
        candidates = mem.build_candidates(event)
        self.assertEqual(len(candidates), 0)

    def test_command_event_still_generates_recent_command_observation(self):
        event = self._make_event(
            kind="command", role="assistant", content='{"command": "ls -la"}', importance=0.7
        )
        candidates = mem.build_candidates(event)
        attributes = [c.attribute for c in candidates]
        self.assertIn("recent_command", attributes)
        self.assertNotIn("recent_summary", attributes)


class TestCmdConsolidate(CliTestBase):
    def test_builds_memories_from_all_observations_matching_filters(self):
        self.local_store.ensure_session("sess_1", client="c", user_id="u1", project_id=None)
        self.local_store.append_observation(
            session_id="sess_1",
            source_event_id="evt_1",
            entity_type="user",
            entity_id="u1",
            attribute="preferred_editor",
            value={"type": "profile", "value": "Neovim"},
            confidence=0.7,
            scope="global",
            extractor_version="test",
        )

        result = self.run_cmd(mem.cmd_consolidate, entity_id="u1", attribute=None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)

    def test_skips_poisoned_project_scope_observation_and_consolidates_the_rest(self):
        """A store already holding a scope="project"/project_id=None observation
        (e.g. left over from before the write-memory fail-fast guard existed)
        must not abort consolidation of the other, valid observations.
        """
        self.local_store.ensure_session("sess_1", client="c", user_id="u1", project_id=None)
        self.local_store.append_observation(
            session_id="sess_1",
            source_event_id="evt_poison",
            entity_type="user",
            entity_id="u1",
            attribute="api_routing",
            value={"type": "reference", "value": "REST"},
            confidence=0.8,
            scope="project",
            project_id=None,
            extractor_version="test",
        )
        self.local_store.append_observation(
            session_id="sess_1",
            source_event_id="evt_ok",
            entity_type="user",
            entity_id="u1",
            attribute="preferred_editor",
            value={"type": "profile", "value": "Neovim"},
            confidence=0.7,
            scope="global",
            extractor_version="test",
        )

        captured_stderr = io.StringIO()
        with patch("sys.stderr", captured_stderr):
            result = self.run_cmd(mem.cmd_consolidate, entity_id="u1", attribute=None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        active = self.markdown_store.search(type="profile")
        titles = [m["title"] for m in active]
        self.assertIn("Preferred Editor", titles)
        self.assertIn("api_routing", captured_stderr.getvalue())


class TestCmdSearch(CliTestBase):
    def _seed_memory(self, **overrides):
        defaults = {
            "type": "profile",
            "entity_type": "user",
            "entity_id": "default",
            "key": "preferred_editor",
            "scope": "global",
            "project_id": None,
            "summary": "好みのエディタ: Neovim",
        }
        defaults.update(overrides)
        self.markdown_store.upsert_from_observation(**defaults)

    def test_search_returns_matching_memory(self):
        self._seed_memory()

        result = self.run_cmd(mem.cmd_search, session_id=None, query="editor", entity_id=None,
                               memory_type=None, scope=None, project_id=None, limit=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["memories"][0]["title"], "Preferred Editor")

    def test_search_logs_retrieval_when_session_id_given(self):
        self._seed_memory()

        self.run_cmd(mem.cmd_search, session_id="sess_1", query="editor", entity_id=None,
                      memory_type=None, scope=None, project_id=None, limit=10)

        path = self.local_dir / "logs" / "retrieval.jsonl"
        self.assertTrue(path.exists())

    def test_search_respects_limit(self):
        self._seed_memory(key="a", summary="a")
        self._seed_memory(key="b", summary="b")

        result = self.run_cmd(mem.cmd_search, session_id=None, query=None, entity_id=None,
                               memory_type=None, scope=None, project_id=None, limit=1)
        self.assertEqual(result["count"], 1)


class TestCmdGetContext(CliTestBase):
    def test_buckets_memories_by_type(self):
        self.markdown_store.upsert_from_observation(
            type="feedback",
            entity_type="user",
            entity_id="default",
            key="response_language",
            scope="global",
            project_id=None,
            summary="応答は日本語で行う",
        )
        self.markdown_store.upsert_from_observation(
            type="profile",
            entity_type="user",
            entity_id="default",
            key="preferred_editor",
            scope="global",
            project_id=None,
            summary="好みのエディタ: Neovim",
        )

        result = self.run_cmd(mem.cmd_get_context, user_id="default", project_id="my-project")

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["context"]["feedback"]), 1)
        self.assertEqual(len(result["context"]["profile"]), 1)
        self.assertEqual(result["context"]["reference"], [])


class TestCmdForget(CliTestBase):
    def test_archives_memory_and_logs_reason(self):
        created = self.markdown_store.upsert_from_observation(
            type="profile",
            entity_type="user",
            entity_id="default",
            key="preferred_editor",
            scope="global",
            project_id=None,
            summary="好みのエディタ: Neovim",
        )

        result = self.run_cmd(mem.cmd_forget, memory_id=created["id"], reason="user changed setup")

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 1)
        self.assertIsNone(self.markdown_store.read(created["id"]))

        deletions_path = self.local_dir / "logs" / "deletions.jsonl"
        self.assertTrue(deletions_path.exists())


class TestQueueSession(unittest.TestCase):
    """cmd_queue_session() saves a payload to a JSONL file without touching a store."""

    def _run_queue_session(self, queue_dir: Path, **kwargs) -> dict:
        defaults = {
            "session_id": "sess_test123456",
            "client": "claude-code",
            "user_id": "default",
            "project_id": "test-project",
            "user_content": "hello user",
            "assistant_content": "hello assistant",
            "summary": "test summary",
        }
        defaults.update(kwargs)
        args = argparse.Namespace(**defaults)

        with patch.object(mem, "QUEUE_DIR", queue_dir):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                mem.cmd_queue_session(args)
            return json.loads(captured.getvalue())

    def test_queue_session_creates_jsonl_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            result = self._run_queue_session(queue_dir)
            self.assertTrue(result["ok"])
            files = list(queue_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 1)

    def test_queue_session_file_contains_valid_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            self._run_queue_session(
                queue_dir,
                session_id="sess_abcdef123456",
                user_content="my question",
                assistant_content="my answer",
                summary="short summary",
            )
            files = list(queue_dir.glob("*.jsonl"))
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["session_id"], "sess_abcdef123456")
            self.assertEqual(data["user_content"], "my question")
            self.assertEqual(data["assistant_content"], "my answer")
            self.assertEqual(data["summary"], "short summary")
            self.assertIn("queued_at", data)

    def test_queue_session_filename_contains_session_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            self._run_queue_session(queue_dir, session_id="sess_uniqueid9999")
            files = list(queue_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            self.assertIn("sess_uniqueid999", files[0].name)


class TestFlushQueue(CliTestBase):
    """cmd_flush_queue() reads JSONL files and writes to the stores, then deletes them."""

    def _queue_one(self, queue_dir: Path, session_id: str = "sess_flush00000000") -> Path:
        queue_dir.mkdir(parents=True, exist_ok=True)
        fname = queue_dir / f"20240101T000000000000_{session_id[:16]}.jsonl"
        payload = {
            "session_id": session_id,
            "client": "claude-code",
            "user_id": "default",
            "project_id": "test-proj",
            "user_content": "test user content",
            "assistant_content": "test assistant content",
            "summary": "test summary",
            "queued_at": mem.utc_now(),
        }
        fname.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return fname

    def _run_flush(self, queue_dir: Path) -> dict:
        with patch.object(mem, "QUEUE_DIR", queue_dir):
            return self.run_cmd(mem.cmd_flush_queue)

    def test_flush_queue_empty_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            result = self._run_flush(queue_dir)
            self.assertTrue(result["ok"])
            self.assertEqual(result["flushed"], 0)

    def test_flush_queue_processes_one_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            self._queue_one(queue_dir, "sess_flush11111111")
            result = self._run_flush(queue_dir)
            self.assertTrue(result["ok"])
            self.assertEqual(result["flushed"], 1)

    def test_flush_queue_deletes_processed_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            self._queue_one(queue_dir, "sess_flush22222222")
            self._run_flush(queue_dir)
            files = list(queue_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 0)

    def test_flush_queue_writes_session_to_local_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            session_id = "sess_flush33333333"
            self._queue_one(queue_dir, session_id)
            self._run_flush(queue_dir)
            self.assertIsNotNone(self.local_store.get_session(session_id))

    def test_flush_queue_processes_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            self._queue_one(queue_dir, "sess_flushAAAAAAAA")
            self._queue_one(queue_dir, "sess_flushBBBBBBBB")
            result = self._run_flush(queue_dir)
            self.assertEqual(result["flushed"], 2)
            files = list(queue_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 0)

    def test_flush_queue_skips_poisoned_observation_and_still_completes(self):
        """A poisoned observation (scope="project", project_id=None) already
        sitting in the local store for the queued session must not abort the
        drain of the rest of that session's observations.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            session_id = "sess_flushCCCCCCCC"
            self.local_store.ensure_session(
                session_id, client="claude-code", user_id="default", project_id=None
            )
            self.local_store.append_observation(
                session_id=session_id,
                source_event_id="evt_poison",
                entity_type="user",
                entity_id="default",
                attribute="api_routing",
                value={"type": "reference", "value": "REST"},
                confidence=0.8,
                scope="project",
                project_id=None,
                extractor_version="test",
            )

            queue_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": session_id,
                "client": "claude-code",
                "user_id": "default",
                "project_id": None,
                "user_content": "I like Neovim",
                "assistant_content": "",
                "summary": "test summary",
                "queued_at": mem.utc_now(),
            }
            (queue_dir / f"20240101T000000000000_{session_id[:16]}.jsonl").write_text(
                json.dumps(payload) + "\n", encoding="utf-8"
            )

            result = self._run_flush(queue_dir)

            self.assertTrue(result["ok"])
            self.assertEqual(result["flushed"], 1)
            active = self.markdown_store.search(type="profile")
            titles = [m["title"] for m in active]
            self.assertIn("Preferred Editor", titles)


class TestFlushQueueIfPossible(unittest.TestCase):
    """flush_queue_if_possible() silently ignores all errors."""

    def test_returns_zero_when_queue_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = Path(tmpdir) / "vault"
            local_dir = Path(tmpdir) / "local"
            queue_dir = Path(tmpdir) / "queue"
            with patch.object(mem, "QUEUE_DIR", queue_dir):
                result = mem.flush_queue_if_possible(vault_dir, local_dir)
            self.assertEqual(result, 0)

    def test_flushes_queued_files_into_stores(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = Path(tmpdir) / "vault"
            local_dir = Path(tmpdir) / "local"
            queue_dir = Path(tmpdir) / "queue"
            queue_dir.mkdir(parents=True)
            payload = {
                "session_id": "sess_fqip",
                "client": "claude-code",
                "user_id": "default",
                "project_id": None,
                "user_content": "hi",
                "assistant_content": "hello",
                "summary": "s",
                "queued_at": mem.utc_now(),
            }
            (queue_dir / "one.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with patch.object(mem, "QUEUE_DIR", queue_dir):
                result = mem.flush_queue_if_possible(vault_dir, local_dir)

            self.assertEqual(result, 1)

    def test_never_raises(self):
        with patch("memory.LocalPipelineStore", side_effect=RuntimeError("boom")):
            with tempfile.TemporaryDirectory() as tmpdir:
                queue_dir = Path(tmpdir) / "queue"
                queue_dir.mkdir(parents=True)
                (queue_dir / "one.jsonl").write_text('{"session_id": "s"}\n', encoding="utf-8")
                with patch.object(mem, "QUEUE_DIR", queue_dir):
                    result = mem.flush_queue_if_possible(Path("/some/vault"), Path("/some/local"))
            self.assertEqual(result, 0)


class TestCmdCleanup(CliTestBase):
    """cmd_cleanup() removes stale recent_summary memories/observations and dedupes superseded."""

    def _seed(self):
        self.local_store.ensure_session("sess_cln", client="c", user_id="u1", project_id="p1")
        self.local_store.append_observation(
            session_id="sess_cln",
            source_event_id="evt_summary",
            entity_type="project",
            entity_id="p1",
            attribute="recent_summary",
            value={"value": "summary text"},
            confidence=0.8,
            scope="project",
            extractor_version="test",
        )
        self.local_store.append_observation(
            session_id="sess_cln",
            source_event_id="evt_editor",
            entity_type="user",
            entity_id="u1",
            attribute="preferred_editor",
            value={"value": "Neovim"},
            confidence=0.8,
            scope="global",
            extractor_version="test",
        )

        # recent_summary memory (should be deleted unconditionally)
        self.markdown_store.upsert_from_observation(
            type="reference",
            entity_type="project",
            entity_id="p1",
            key="recent_summary",
            scope="project",
            project_id="p1",
            summary="summary",
        )

        # kept memory
        self.markdown_store.upsert_from_observation(
            type="profile",
            entity_type="user",
            entity_id="u1",
            key="preferred_language_runtime",
            scope="global",
            project_id=None,
            summary="Python",
        )

        # repeated content changes fold into the same file's history rather
        # than creating separate records (see MarkdownMemoryStore)
        for value in ("Vim", "Emacs"):
            self.markdown_store.upsert_from_observation(
                type="profile",
                entity_type="user",
                entity_id="u1",
                key="preferred_editor",
                scope="global",
                project_id=None,
                summary=f"好みのエディタ: {value}",
            )

    def test_deletes_recent_summary_memories(self):
        self._seed()
        result = self.run_cmd(mem.cmd_cleanup)
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted_summary_memories"], 1)

    def test_deletes_recent_summary_observations(self):
        self._seed()
        result = self.run_cmd(mem.cmd_cleanup)
        self.assertEqual(result["deleted_summary_observations"], 1)
        remaining = self.local_store.iter_observations("sess_cln")
        attributes = [o["attribute"] for o in remaining]
        self.assertNotIn("recent_summary", attributes)
        self.assertIn("preferred_editor", attributes)

    def test_preserves_other_memories(self):
        self._seed()
        self.run_cmd(mem.cmd_cleanup)
        active = self.markdown_store.search(type="profile")
        titles = [m["title"] for m in active]
        self.assertIn("Preferred Language Runtime", titles)

    def test_returns_ok_true_with_empty_store(self):
        result = self.run_cmd(mem.cmd_cleanup)
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted_summary_memories"], 0)


class TestListUnextractedSubcommand(unittest.TestCase):
    """list-unextracted subcommand should be registered and return JSON with sessions."""

    def test_list_unextracted_subcommand_exists(self):
        parser = mem.build_parser()
        args = parser.parse_args(["list-unextracted"])
        self.assertEqual(args.command, "list-unextracted")
        self.assertTrue(callable(args.func))

    def test_list_unextracted_func_is_cmd_list_unextracted(self):
        parser = mem.build_parser()
        args = parser.parse_args(["list-unextracted"])
        self.assertIs(args.func, mem.cmd_list_unextracted)

    def test_list_unextracted_default_limit_is_10(self):
        parser = mem.build_parser()
        args = parser.parse_args(["list-unextracted"])
        self.assertEqual(args.limit, 10)


class TestListUnextractedCmd(CliTestBase):
    def test_returns_ok_and_sessions(self):
        self.local_store.ensure_session("sess_lu01", client="claude-code", user_id="u1", project_id=None)
        self.local_store.update_session("sess_lu01", summary="some summary")

        result = self.run_cmd(mem.cmd_list_unextracted, limit=10)

        self.assertTrue(result["ok"])
        ids = [s["id"] for s in result["sessions"]]
        self.assertIn("sess_lu01", ids)

    def test_excludes_already_extracted(self):
        self.local_store.ensure_session("sess_done01", client="claude-code", user_id="u1", project_id=None)
        self.local_store.update_session("sess_done01", summary="done")
        self.local_store.mark_extracted("sess_done01")

        result = self.run_cmd(mem.cmd_list_unextracted, limit=10)

        ids = [s["id"] for s in result["sessions"]]
        self.assertNotIn("sess_done01", ids)


class TestWriteMemorySubcommand(unittest.TestCase):
    """write-memory subcommand should be registered with the expected defaults."""

    def test_write_memory_subcommand_exists(self):
        parser = mem.build_parser()
        args = parser.parse_args([
            "write-memory",
            "--session-id", "sess_test01",
            "--memory-type", "profile",
            "--key", "preferred_editor",
            "--summary", "好みのエディタ: Neovim",
        ])
        self.assertEqual(args.command, "write-memory")
        self.assertTrue(callable(args.func))
        self.assertIs(args.func, mem.cmd_write_memory)

    def test_write_memory_default_confidence_and_scope(self):
        parser = mem.build_parser()
        args = parser.parse_args([
            "write-memory",
            "--session-id", "sess_test02",
            "--memory-type", "feedback",
            "--key", "response_language",
            "--summary", "応答は日本語で行う",
        ])
        self.assertAlmostEqual(args.confidence, 0.8)
        self.assertEqual(args.scope, "global")
        self.assertEqual(args.entity_type, "user")
        self.assertEqual(args.entity_id, "default")


class TestCmdWriteMemory(CliTestBase):
    def test_creates_observation_and_active_memory(self):
        result = self.run_cmd(
            mem.cmd_write_memory,
            session_id="sess_wm01",
            memory_type="profile",
            entity_type="user",
            entity_id="default",
            key="preferred_editor",
            summary="ユーザーは Neovim を好む",
            confidence=0.9,
            scope="global",
            project_id=None,
        )

        self.assertTrue(result["ok"])
        self.assertIn("observation_id", result)
        self.assertIn("event_id", result)

        active = self.markdown_store.search(type="profile")
        matching = [m for m in active if m["title"] == "Preferred Editor"]
        self.assertEqual(len(matching), 1)

        observations = self.local_store.iter_observations("sess_wm01")
        self.assertEqual(len(observations), 1)

    def test_project_scope_with_project_id_writes_a_project_scoped_memory(self):
        result = self.run_cmd(
            mem.cmd_write_memory,
            session_id="sess_wm02",
            memory_type="reference",
            entity_type="user",
            entity_id="default",
            key="api_routing",
            summary="APIルーティング方針: REST",
            confidence=0.8,
            scope="project",
            project_id="lab-web",
        )

        self.assertTrue(result["ok"])
        matching = self.markdown_store.search(scope="project", project_id="lab-web")
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["project_id"], "lab-web")

        global_matching = self.markdown_store.search(scope="global")
        self.assertEqual(global_matching, [])

    def test_project_scope_without_project_id_raises_instead_of_writing_a_global_memory(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(
                mem.cmd_write_memory,
                session_id="sess_wm03",
                memory_type="reference",
                entity_type="user",
                entity_id="default",
                key="api_routing",
                summary="APIルーティング方針: REST",
                confidence=0.8,
                scope="project",
                project_id=None,
            )

        self.assertEqual(self.markdown_store.search(), [])

    def test_project_scope_without_project_id_writes_nothing_to_local_store(self):
        """Validation must happen before any local-store writes (fail fast).

        Otherwise a poisoned observation (scope="project", project_id=None)
        is left behind in the local store even though the CLI call itself
        raised, and later trips up batch consolidation (see
        ``TestConsolidateResilientToPoisonedObservations``).
        """
        with self.assertRaises(SystemExit):
            self.run_cmd(
                mem.cmd_write_memory,
                session_id="sess_wm04",
                memory_type="reference",
                entity_type="user",
                entity_id="default",
                key="api_routing",
                summary="APIルーティング方針: REST",
                confidence=0.8,
                scope="project",
                project_id=None,
            )

        self.assertEqual(self.local_store.iter_observations("sess_wm04"), [])
        self.assertEqual(self.local_store.iter_events("sess_wm04"), [])


class TestMarkExtractedSubcommand(unittest.TestCase):
    """mark-extracted subcommand should be registered."""

    def test_mark_extracted_subcommand_exists(self):
        parser = mem.build_parser()
        args = parser.parse_args(["mark-extracted", "--session-id", "sess_x01"])
        self.assertEqual(args.command, "mark-extracted")
        self.assertIs(args.func, mem.cmd_mark_extracted)


class TestCmdMarkExtracted(CliTestBase):
    def test_marks_session_extracted(self):
        self.local_store.ensure_session("sess_me01", client="c", user_id="u1", project_id=None)
        self.local_store.update_session("sess_me01", summary="some summary")

        result = self.run_cmd(mem.cmd_mark_extracted, session_id="sess_me01")

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 1)
        session = self.local_store.get_session("sess_me01")
        self.assertIsNotNone(session["extracted_at"])

    def test_already_extracted_returns_zero(self):
        self.local_store.ensure_session("sess_already", client="c", user_id="u1", project_id=None)
        self.local_store.mark_extracted("sess_already")

        result = self.run_cmd(mem.cmd_mark_extracted, session_id="sess_already")

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 0)


class TestCleanupSubcommandRegistered(unittest.TestCase):
    """cleanup subcommand should be registered in build_parser()."""

    def test_cleanup_subcommand_exists(self):
        parser = mem.build_parser()
        args = parser.parse_args(["cleanup"])
        self.assertEqual(args.command, "cleanup")
        self.assertIs(args.func, mem.cmd_cleanup)


class TestDeprecatedDbFlag(unittest.TestCase):
    """--db is accepted but ignored (deprecated no-op)."""

    def test_db_flag_is_accepted(self):
        parser = mem.build_parser()
        args = parser.parse_args(["--db", "/some/legacy/path.db", "cleanup"])
        self.assertEqual(args.db, Path("/some/legacy/path.db"))

    def test_db_flag_defaults_to_none(self):
        parser = mem.build_parser()
        args = parser.parse_args(["cleanup"])
        self.assertIsNone(args.db)


class TestVaultAndLocalDirFlags(unittest.TestCase):
    """--vault and --local-dir override the resolved directories."""

    def test_vault_flag_is_accepted(self):
        parser = mem.build_parser()
        args = parser.parse_args(["--vault", "/some/vault", "cleanup"])
        self.assertEqual(args.vault, Path("/some/vault"))

    def test_local_dir_flag_is_accepted(self):
        parser = mem.build_parser()
        args = parser.parse_args(["--local-dir", "/some/local", "cleanup"])
        self.assertEqual(args.local_dir, Path("/some/local"))


if __name__ == "__main__":
    unittest.main()
