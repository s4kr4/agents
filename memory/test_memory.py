#!/usr/bin/env python3
"""Tests for queue-based memory system (file-based fallback when DB is not writable)."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_MEMORY_DIR = Path(__file__).resolve().parent

# Import memory.py directly to avoid confusion with the memory/ namespace package
import importlib.util as _ilu
import types as _types

_spec = _ilu.spec_from_file_location("memory_module", _MEMORY_DIR / "memory.py")
mem = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
# Register under both names so that @dataclass works correctly
sys.modules["memory_module"] = mem
_spec.loader.exec_module(mem)  # type: ignore[union-attr]


def make_temp_db() -> Path:
    """Create a temporary SQLite database with the schema applied."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = mem.connect_readwrite(db_path)
    conn.close()
    return db_path


class TestConnectReadwrite(unittest.TestCase):
    """connect_readwrite() creates a writable connection with schema applied."""

    def test_connect_readwrite_creates_db_and_returns_connection(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Act
            conn = mem.connect_readwrite(db_path)

            # Assert
            self.assertIsInstance(conn, sqlite3.Connection)
            conn.close()
            self.assertTrue(db_path.exists())

    def test_connect_readwrite_applies_schema(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Act
            conn = mem.connect_readwrite(db_path)

            # Assert: sessions table must exist
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            self.assertIsNotNone(row)
            conn.close()

    def test_connect_readwrite_creates_parent_dirs(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "nested" / "dir" / "test.db"

            # Act
            conn = mem.connect_readwrite(db_path)

            # Assert
            self.assertTrue(db_path.exists())
            conn.close()

    def test_connect_readwrite_sets_temp_store_directory_to_tmp(self):
        # /var/tmp/ がブロックされる sandbox 環境でも動作するよう /tmp/ を使う
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Act
            conn = mem.connect_readwrite(db_path)

            # Assert: temp_store_directory が /tmp に設定されている
            row = conn.execute("PRAGMA temp_store_directory").fetchone()
            self.assertEqual(row[0], "/tmp")
            conn.close()


class TestConnectReadonly(unittest.TestCase):
    """connect_readonly() opens an existing DB without schema changes."""

    def test_connect_readonly_opens_existing_db(self):
        # Arrange
        db_path = make_temp_db()

        try:
            # Act
            conn = mem.connect_readonly(db_path)

            # Assert
            self.assertIsInstance(conn, sqlite3.Connection)
            conn.close()
        finally:
            db_path.unlink(missing_ok=True)

    def test_connect_readonly_raises_when_db_missing(self):
        # Arrange
        db_path = Path("/tmp/nonexistent_test_memory_12345.db")

        # Act / Assert
        with self.assertRaises(FileNotFoundError):
            mem.connect_readonly(db_path)

    def test_connect_readonly_cannot_write(self):
        # Arrange
        db_path = make_temp_db()

        try:
            conn = mem.connect_readonly(db_path)

            # Act / Assert: writing must fail in read-only mode
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO sessions(id, client, user_id, started_at) VALUES('x','c','u','t')"
                )
            conn.close()
        finally:
            db_path.unlink(missing_ok=True)

    def test_connect_readonly_sets_temp_store_directory_to_tmp(self):
        # /var/tmp/ がブロックされる sandbox 環境でも動作するよう /tmp/ を使う
        db_path = make_temp_db()

        try:
            # Act
            conn = mem.connect_readonly(db_path)

            # Assert: temp_store_directory が /tmp に設定されている
            row = conn.execute("PRAGMA temp_store_directory").fetchone()
            self.assertEqual(row[0], "/tmp")
            conn.close()
        finally:
            db_path.unlink(missing_ok=True)

    def test_connect_backward_compat(self):
        """connect() is an alias for connect_readwrite()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = mem.connect(db_path)
            self.assertIsInstance(conn, sqlite3.Connection)
            conn.close()


class TestQueueSession(unittest.TestCase):
    """cmd_queue_session() saves a payload to a JSONL file without touching SQLite."""

    def _run_queue_session(self, queue_dir: Path, **kwargs) -> dict:
        import argparse
        import io

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
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"

            # Act
            result = self._run_queue_session(queue_dir)

            # Assert
            self.assertTrue(result["ok"])
            files = list(queue_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 1)

    def test_queue_session_file_contains_valid_payload(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"

            # Act
            self._run_queue_session(
                queue_dir,
                session_id="sess_abcdef123456",
                user_content="my question",
                assistant_content="my answer",
                summary="short summary",
            )

            # Assert
            files = list(queue_dir.glob("*.jsonl"))
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["session_id"], "sess_abcdef123456")
            self.assertEqual(data["user_content"], "my question")
            self.assertEqual(data["assistant_content"], "my answer")
            self.assertEqual(data["summary"], "short summary")
            self.assertIn("queued_at", data)

    def test_queue_session_does_not_touch_sqlite(self):
        """cmd_queue_session must not open any SQLite connection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"

            with patch("sqlite3.connect") as mock_connect:
                self._run_queue_session(queue_dir)
                mock_connect.assert_not_called()

    def test_queue_session_filename_contains_session_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / "queue"
            self._run_queue_session(queue_dir, session_id="sess_uniqueid9999")

            files = list(queue_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            # filename should contain first 16 chars of session_id
            self.assertIn("sess_uniqueid999", files[0].name)


class TestFlushQueue(unittest.TestCase):
    """cmd_flush_queue() reads JSONL files and writes to SQLite, then deletes them."""

    def _queue_one(self, queue_dir: Path, session_id: str = "sess_flush00000000") -> Path:
        """Write a queue JSONL file directly."""
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

    def _run_flush(self, db_path: Path, queue_dir: Path) -> dict:
        import argparse
        import io

        args = argparse.Namespace(db=db_path)
        with patch.object(mem, "QUEUE_DIR", queue_dir):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                mem.cmd_flush_queue(args)
            return json.loads(captured.getvalue())

    def test_flush_queue_empty_queue(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = make_temp_db()
            queue_dir = Path(tmpdir) / "queue"

            try:
                # Act
                result = self._run_flush(db_path, queue_dir)

                # Assert
                self.assertTrue(result["ok"])
                self.assertEqual(result["flushed"], 0)
            finally:
                db_path.unlink(missing_ok=True)

    def test_flush_queue_processes_one_file(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = make_temp_db()
            queue_dir = Path(tmpdir) / "queue"
            self._queue_one(queue_dir, "sess_flush11111111")

            try:
                # Act
                result = self._run_flush(db_path, queue_dir)

                # Assert
                self.assertTrue(result["ok"])
                self.assertEqual(result["flushed"], 1)
            finally:
                db_path.unlink(missing_ok=True)

    def test_flush_queue_deletes_processed_files(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = make_temp_db()
            queue_dir = Path(tmpdir) / "queue"
            self._queue_one(queue_dir, "sess_flush22222222")

            try:
                # Act
                self._run_flush(db_path, queue_dir)

                # Assert: file should be deleted after flush
                files = list(queue_dir.glob("*.jsonl"))
                self.assertEqual(len(files), 0)
            finally:
                db_path.unlink(missing_ok=True)

    def test_flush_queue_writes_session_to_db(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = make_temp_db()
            queue_dir = Path(tmpdir) / "queue"
            session_id = "sess_flush33333333"
            self._queue_one(queue_dir, session_id)

            try:
                # Act
                self._run_flush(db_path, queue_dir)

                # Assert: session exists in DB
                conn = mem.connect_readonly(db_path)
                row = conn.execute(
                    "SELECT id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                conn.close()
                self.assertIsNotNone(row)
            finally:
                db_path.unlink(missing_ok=True)

    def test_flush_queue_processes_multiple_files(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = make_temp_db()
            queue_dir = Path(tmpdir) / "queue"
            self._queue_one(queue_dir, "sess_flushAAAAAAAA")
            self._queue_one(queue_dir, "sess_flushBBBBBBBB")

            try:
                # Act
                result = self._run_flush(db_path, queue_dir)

                # Assert
                self.assertEqual(result["flushed"], 2)
                files = list(queue_dir.glob("*.jsonl"))
                self.assertEqual(len(files), 0)
            finally:
                db_path.unlink(missing_ok=True)


class TestFlushQueueIfPossible(unittest.TestCase):
    """flush_queue_if_possible() silently ignores all errors."""

    def test_returns_zero_when_db_missing(self):
        db_path = Path("/tmp/nonexistent_memory_test_999.db")
        result = mem.flush_queue_if_possible(db_path)
        self.assertEqual(result, 0)

    def test_returns_zero_when_queue_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = make_temp_db()
            queue_dir = Path(tmpdir) / "queue"

            try:
                with patch.object(mem, "QUEUE_DIR", queue_dir):
                    result = mem.flush_queue_if_possible(db_path)
                self.assertIsInstance(result, int)
            finally:
                db_path.unlink(missing_ok=True)

    def test_never_raises(self):
        """flush_queue_if_possible must never propagate exceptions."""
        with patch.object(mem, "connect_readwrite", side_effect=RuntimeError("boom")):
            # Should not raise
            result = mem.flush_queue_if_possible(Path("/some/path.db"))
            self.assertEqual(result, 0)


class TestBuildCandidatesNoRecentSummary(unittest.TestCase):
    """build_candidates() should NOT generate recent_summary observations from summary events."""

    def _make_event(self, **kwargs) -> sqlite3.Row:
        """Create a sqlite3.Row-like object for testing."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
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
        defaults.update(kwargs)
        cols = ", ".join(defaults.keys())
        placeholders = ", ".join("?" for _ in defaults)
        conn.execute(f"CREATE TABLE t ({cols})")
        conn.execute(f"INSERT INTO t VALUES ({placeholders})", list(defaults.values()))
        row = conn.execute("SELECT * FROM t").fetchone()
        conn.close()
        return row

    def test_summary_event_does_not_generate_recent_summary_observation(self):
        # Arrange
        event = self._make_event(kind="summary", content="Some session summary.")

        # Act
        candidates = mem.build_candidates(event)

        # Assert: no candidate should have attribute 'recent_summary'
        attributes = [c.attribute for c in candidates]
        self.assertNotIn("recent_summary", attributes)

    def test_summary_event_generates_no_candidates_at_all(self):
        # Arrange: summary event with no user/command content
        event = self._make_event(kind="summary", role="assistant", content="Summary text.")

        # Act
        candidates = mem.build_candidates(event)

        # Assert: summary events should produce zero candidates
        self.assertEqual(len(candidates), 0)

    def test_command_event_still_generates_recent_command_observation(self):
        # Arrange: command event should still work
        event = self._make_event(
            kind="command",
            role="assistant",
            content='{"command": "ls -la"}',
            importance=0.7,
        )

        # Act
        candidates = mem.build_candidates(event)

        # Assert: recent_command observation is still generated
        attributes = [c.attribute for c in candidates]
        self.assertIn("recent_command", attributes)
        self.assertNotIn("recent_summary", attributes)


class TestConsolidateSessionFilter(unittest.TestCase):
    """cmd_end_session consolidate should only process observations from the current session."""

    def _setup_db_with_two_sessions(self) -> tuple[Path, str, str]:
        """Set up a DB with two sessions and observations from each."""
        db_path = make_temp_db()
        conn = mem.connect_readwrite(db_path)

        # Session 1
        sess1_id = "sess_old0000000000"
        conn.execute(
            "INSERT INTO sessions(id, client, user_id, project_id, started_at) VALUES(?,?,?,?,?)",
            (sess1_id, "claude-code", "user1", "proj1", "2024-01-01T00:00:00+00:00"),
        )
        evt1_id = "evt_old0000000000"
        conn.execute(
            "INSERT INTO events(id, session_id, role, kind, content, created_at, importance) VALUES(?,?,?,?,?,?,?)",
            (evt1_id, sess1_id, "user", "message", "I like TypeScript", "2024-01-01T00:00:00+00:00", 0.5),
        )
        obs1_id = "obs_old0000000000"
        conn.execute(
            """
            INSERT INTO observations(id, source_event_id, entity_type, entity_id, attribute, value_json, confidence, scope, observed_at, extractor_version)
            VALUES(?, ?, 'user', 'user1', 'preferred_language_runtime', '{"memory_type":"semantic","value":"TypeScript"}', 0.75, 'global', '2024-01-01T00:00:00+00:00', 'test')
            """,
            (obs1_id, evt1_id),
        )

        # Session 2 (current)
        sess2_id = "sess_new0000000000"
        conn.execute(
            "INSERT INTO sessions(id, client, user_id, project_id, started_at) VALUES(?,?,?,?,?)",
            (sess2_id, "claude-code", "user1", "proj1", "2024-06-01T00:00:00+00:00"),
        )
        evt2_id = "evt_new0000000000"
        conn.execute(
            "INSERT INTO events(id, session_id, role, kind, content, created_at, importance) VALUES(?,?,?,?,?,?,?)",
            (evt2_id, sess2_id, "user", "message", "I like Python", "2024-06-01T00:00:00+00:00", 0.5),
        )
        obs2_id = "obs_new0000000000"
        conn.execute(
            """
            INSERT INTO observations(id, source_event_id, entity_type, entity_id, attribute, value_json, confidence, scope, observed_at, extractor_version)
            VALUES(?, ?, 'user', 'user1', 'preferred_language_runtime', '{"memory_type":"semantic","value":"Python"}', 0.75, 'global', '2024-06-01T00:00:00+00:00', 'test')
            """,
            (obs2_id, evt2_id),
        )

        conn.commit()
        conn.close()
        return db_path, sess2_id, obs1_id

    def test_consolidate_in_end_session_only_processes_current_session_observations(self):
        """When --consolidate is used in end-session, only current session's observations become memories."""
        import argparse
        import io

        db_path, sess2_id, obs1_id = self._setup_db_with_two_sessions()
        try:
            # Ensure session 2 is not ended yet
            conn = mem.connect_readwrite(db_path)
            conn.execute(
                "UPDATE sessions SET ended_at = '2024-01-01T01:00:00+00:00' WHERE id = 'sess_old0000000000'"
            )
            conn.commit()
            conn.close()

            args = argparse.Namespace(
                db=db_path,
                session_id=sess2_id,
                summary="test summary",
                append_summary_event=False,
                extract=False,
                consolidate=True,
            )

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                mem.cmd_end_session(args)

            # Only session 2's observation (obs2) should have been consolidated into a memory.
            # The old session 1 observation (obs1) should NOT create a new memory in this run.
            result = json.loads(captured.getvalue())
            self.assertTrue(result["ok"])

            conn = mem.connect_readonly(db_path)
            # Check that memory_sources only contain obs from session 2
            sources = conn.execute(
                """
                SELECT ms.observation_id
                FROM memory_sources ms
                JOIN observations o ON o.id = ms.observation_id
                JOIN events e ON e.id = o.source_event_id
                WHERE e.session_id = 'sess_old0000000000'
                  AND ms.memory_id IN (SELECT id FROM memories WHERE status = 'active')
                """
            ).fetchall()
            conn.close()
            # No active memories should have sources from session 1 after this run
            self.assertEqual(len(sources), 0)
        finally:
            db_path.unlink(missing_ok=True)


class TestCmdCleanup(unittest.TestCase):
    """cmd_cleanup() removes stale recent_summary memories and observations."""

    def _setup_db_with_recent_summary_data(self) -> Path:
        """Set up a DB with recent_summary memories (active and superseded)."""
        db_path = make_temp_db()
        conn = mem.connect_readwrite(db_path)

        # Insert a session and events (required for FOREIGN KEY constraints)
        conn.execute(
            "INSERT INTO sessions(id, client, user_id, project_id, started_at) VALUES(?,?,?,?,?)",
            ("sess_cln0000000000", "claude-code", "user1", "proj1", "2024-01-01T00:00:00+00:00"),
        )
        for i in range(3):
            evt_id = f"evt_cln{i:013d}"
            conn.execute(
                "INSERT INTO events(id, session_id, role, kind, content, created_at, importance) VALUES(?,?,?,?,?,?,?)",
                (evt_id, "sess_cln0000000000", "assistant", "summary", f"summary {i}", "2024-01-01T00:00:00+00:00", 0.9),
            )
        # Insert some recent_summary observations
        for i in range(3):
            obs_id = f"obs_cln{i:013d}"
            evt_id = f"evt_cln{i:013d}"
            conn.execute(
                """
                INSERT INTO observations(id, source_event_id, entity_type, entity_id, attribute, value_json, confidence, scope, observed_at, extractor_version)
                VALUES(?, ?, 'project', 'proj1', 'recent_summary', ?, 0.8, 'project', '2024-01-01T00:00:00+00:00', 'test')
                """,
                (obs_id, evt_id, json.dumps({"value": f"summary {i}"})),
            )
        # Insert superseded recent_summary memories
        for i in range(5):
            mem_id = f"mem_cln{i:013d}"
            conn.execute(
                """
                INSERT INTO memories(id, memory_type, entity_type, entity_id, key, value_json, summary, confidence, salience, scope, project_id, status, valid_from, created_at, updated_at)
                VALUES(?, 'episodic', 'project', 'proj1', 'recent_summary', ?, 'summary', 0.8, 0.8, 'project', 'proj1', 'superseded', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')
                """,
                (mem_id, json.dumps({"value": f"old summary {i}"})),
            )
        # Insert 1 active recent_summary memory
        conn.execute(
            """
            INSERT INTO memories(id, memory_type, entity_type, entity_id, key, value_json, summary, confidence, salience, scope, project_id, status, valid_from, created_at, updated_at)
            VALUES('mem_active_summary', 'episodic', 'project', 'proj1', 'recent_summary', '{"value":"current"}', 'summary', 0.8, 0.8, 'project', 'proj1', 'active', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')
            """,
        )
        # Insert a different key memory that should NOT be deleted
        conn.execute(
            """
            INSERT INTO memories(id, memory_type, entity_type, entity_id, key, value_json, summary, confidence, salience, scope, project_id, status, valid_from, created_at, updated_at)
            VALUES('mem_keep_lang', 'semantic', 'user', 'user1', 'preferred_language_runtime', '{"value":"Python"}', 'Python', 0.9, 0.9, 'global', NULL, 'active', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')
            """,
        )
        conn.commit()
        conn.close()
        return db_path

    def _run_cleanup(self, db_path: Path) -> dict:
        import argparse
        import io

        args = argparse.Namespace(db=db_path)
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            mem.cmd_cleanup(args)
        return json.loads(captured.getvalue())

    def test_cleanup_deletes_superseded_recent_summary_memories(self):
        # Arrange
        db_path = self._setup_db_with_recent_summary_data()
        try:
            # Act
            result = self._run_cleanup(db_path)

            # Assert
            self.assertTrue(result["ok"])
            self.assertEqual(result["deleted_summary_memories"], 6)  # 5 superseded + 1 active
        finally:
            db_path.unlink(missing_ok=True)

    def test_cleanup_deletes_recent_summary_observations(self):
        # Arrange
        db_path = self._setup_db_with_recent_summary_data()
        try:
            # Act
            result = self._run_cleanup(db_path)

            # Assert
            self.assertTrue(result["ok"])
            self.assertEqual(result["deleted_summary_observations"], 3)
        finally:
            db_path.unlink(missing_ok=True)

    def test_cleanup_deduplicates_superseded(self):
        # Arrange
        db_path = self._setup_db_with_recent_summary_data()
        try:
            # Act
            result = self._run_cleanup(db_path)

            # Assert
            self.assertTrue(result["ok"])
            self.assertIn("deleted_duplicate_superseded", result)
        finally:
            db_path.unlink(missing_ok=True)

    def test_cleanup_preserves_other_memories(self):
        # Arrange
        db_path = self._setup_db_with_recent_summary_data()
        try:
            # Act
            self._run_cleanup(db_path)

            # Assert: preferred_language_runtime memory is still there
            conn = mem.connect_readonly(db_path)
            row = conn.execute(
                "SELECT id FROM memories WHERE id = 'mem_keep_lang'"
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
        finally:
            db_path.unlink(missing_ok=True)

    def test_cleanup_returns_ok_true(self):
        # Arrange
        db_path = make_temp_db()
        try:
            # Act
            result = self._run_cleanup(db_path)

            # Assert: even with empty DB, returns ok
            self.assertTrue(result["ok"])
        finally:
            db_path.unlink(missing_ok=True)


class TestCleanupSubcommandRegistered(unittest.TestCase):
    """cleanup subcommand should be registered in build_parser()."""

    def test_cleanup_subcommand_exists(self):
        # Arrange
        parser = mem.build_parser()

        # Act: parse cleanup command (should not raise)
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            args = parser.parse_args(["--db", f.name, "cleanup"])

        # Assert
        self.assertEqual(args.command, "cleanup")
        self.assertTrue(callable(args.func))

    def test_cleanup_func_is_cmd_cleanup(self):
        # Arrange
        parser = mem.build_parser()

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            args = parser.parse_args(["--db", f.name, "cleanup"])

        # Assert
        self.assertIs(args.func, mem.cmd_cleanup)


if __name__ == "__main__":
    unittest.main()
