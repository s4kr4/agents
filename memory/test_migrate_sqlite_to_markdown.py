#!/usr/bin/env python3
"""Tests for the one-shot SQLite -> Markdown/local-files migration script."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_store import LocalPipelineStore
from markdown_store import MarkdownMemoryStore, canonical_memory_id
from migrate_sqlite_to_markdown import migrate

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, client TEXT NOT NULL, user_id TEXT NOT NULL, project_id TEXT,
    started_at TEXT NOT NULL, ended_at TEXT, summary TEXT, extracted_at TEXT
);
CREATE TABLE events (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5
);
CREATE TABLE observations (
    id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, attribute TEXT NOT NULL, value_json TEXT NOT NULL,
    confidence REAL NOT NULL, scope TEXT NOT NULL, observed_at TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);
CREATE TABLE memories (
    id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, key TEXT NOT NULL, value_json TEXT NOT NULL, summary TEXT NOT NULL,
    confidence REAL NOT NULL, salience REAL NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
    project_id TEXT, status TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE memory_sources (
    memory_id TEXT NOT NULL, observation_id TEXT NOT NULL, weight REAL NOT NULL,
    PRIMARY KEY(memory_id, observation_id)
);
CREATE TABLE retrieval_logs (
    id TEXT PRIMARY KEY, session_id TEXT, query TEXT NOT NULL,
    returned_memory_ids TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE deletions (
    id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _build_fixture_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT INTO sessions VALUES('sess_1','claude-code','u1','p1',"
        "'2024-01-01T00:00:00+00:00','2024-01-01T00:05:00+00:00','a summary','2024-01-01T00:06:00+00:00')"
    )
    conn.execute(
        "INSERT INTO events VALUES('evt_1','sess_1','user','message','I like Neovim',"
        "'2024-01-01T00:00:00+00:00',0.5)"
    )
    conn.execute(
        "INSERT INTO observations VALUES('obs_1','evt_1','user','u1','preferred_editor',?,"
        "0.7,'global','2024-01-01T00:00:30+00:00','rule-based-v1')",
        (json.dumps({"memory_type": "semantic", "value": "Neovim"}),),
    )
    conn.execute(
        "INSERT INTO memories VALUES('mem_active_1','semantic','user','u1','preferred_editor',?,"
        "'好みのエディタ: Neovim',0.7,0.7,'global',NULL,'active','2024-01-01T00:01:00+00:00',NULL,"
        "'2024-01-01T00:01:00+00:00','2024-01-01T00:01:00+00:00')",
        (json.dumps({"value": "Neovim"}),),
    )
    conn.execute(
        "INSERT INTO memories VALUES('mem_superseded_1','semantic','user','u1','preferred_editor',?,"
        "'好みのエディタ: Vim',0.6,0.6,'global',NULL,'superseded','2023-12-01T00:00:00+00:00',"
        "'2024-01-01T00:01:00+00:00','2023-12-01T00:00:00+00:00','2024-01-01T00:01:00+00:00')",
        (json.dumps({"value": "Vim"}),),
    )
    conn.execute("INSERT INTO memory_sources VALUES('mem_active_1','obs_1',0.7)")
    conn.commit()
    conn.close()


class MigrateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.db_path = base / "memory.db"
        self.vault_dir = base / "vault"
        self.local_dir = base / "local"
        _build_fixture_db(self.db_path)

    def tearDown(self):
        self._tmpdir.cleanup()


class TestMigrateDryRun(MigrateTestBase):
    def test_dry_run_does_not_write_any_files(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=False)

        self.assertFalse(
            (self.vault_dir / "memory").exists() and any((self.vault_dir / "memory").iterdir())
            if (self.vault_dir / "memory").exists()
            else False
        )
        self.assertFalse(self.local_dir.exists())

    def test_dry_run_reports_planned_counts(self):
        summary = migrate(self.db_path, self.vault_dir, self.local_dir, apply=False)

        self.assertFalse(summary["applied"])
        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["observations"], 1)
        self.assertEqual(summary["memories"], 2)


class TestMigrateApply(MigrateTestBase):
    def test_apply_writes_session(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        local_store = LocalPipelineStore(self.local_dir)
        session = local_store.get_session("sess_1")
        self.assertIsNotNone(session)
        self.assertEqual(session["summary"], "a summary")
        self.assertEqual(session["extracted_at"], "2024-01-01T00:06:00+00:00")

    def test_apply_writes_event(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        local_store = LocalPipelineStore(self.local_dir)
        events = local_store.iter_events("sess_1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["content"], "I like Neovim")

    def test_apply_writes_observation(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        local_store = LocalPipelineStore(self.local_dir)
        observations = local_store.iter_observations("sess_1")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["attribute"], "preferred_editor")

    def test_apply_writes_memory_with_canonical_id(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "preferred_editor", "global", None)
        record = markdown_store.read(expected_id)
        self.assertIsNotNone(record)
        self.assertIn("Neovim", record["summary"])

    def test_apply_maps_legacy_semantic_memory_type_to_the_profile_type(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "preferred_editor", "global", None)
        record = markdown_store.read(expected_id)
        self.assertEqual(record["type"], "profile")

    def test_apply_folds_superseded_row_into_the_active_records_history(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "preferred_editor", "global", None)

        record = markdown_store.read(expected_id)
        self.assertIsNotNone(record)
        self.assertEqual(len(record["history"]), 1)
        self.assertIn("Vim", record["history"][0])
        self.assertIn("2024-01-01", record["history"][0])

    def test_apply_uses_the_earliest_rows_created_at_date_for_the_logical_key(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "preferred_editor", "global", None)

        record = markdown_store.read(expected_id)
        self.assertEqual(record["created"], "2023-12-01")

    def test_apply_does_not_create_a_separate_file_for_the_superseded_row(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        self.assertEqual(len(markdown_store.iter_all()), 1)

    def test_apply_returns_summary_with_applied_true(self):
        summary = migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)
        self.assertTrue(summary["applied"])
        self.assertEqual(summary["memories"], 2)


def _build_fixture_db_with_long_evidence(db_path: Path, evidence: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT INTO memories VALUES('mem_long_evidence','semantic','user','u1','primary_os',?,"
        "'主な OS: Arch Linux',0.9,0.9,'global',NULL,'active','2024-01-01T00:00:00+00:00',NULL,"
        "'2024-01-01T00:00:00+00:00','2024-01-01T00:00:00+00:00')",
        (json.dumps({"value": "Arch Linux", "evidence": evidence}),),
    )
    conn.commit()
    conn.close()


class TestMigrateEvidenceExcerpt(unittest.TestCase):
    """Long legacy ``evidence`` text is excerpted, not dumped verbatim, into the body."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.db_path = base / "memory.db"
        self.vault_dir = base / "vault"
        self.local_dir = base / "local"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_long_evidence_is_truncated_in_the_migrated_body(self):
        long_evidence = "captured CLI transcript line. " * 50
        _build_fixture_db_with_long_evidence(self.db_path, long_evidence)

        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "primary_os", "global", None)
        record = markdown_store.read(expected_id)
        self.assertIsNotNone(record)
        self.assertLess(len(record["summary"]), len(long_evidence))
        self.assertIn("...", record["summary"])

    def test_short_evidence_is_kept_in_full(self):
        short_evidence = "Arch Linuxを使っています"
        _build_fixture_db_with_long_evidence(self.db_path, short_evidence)

        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "primary_os", "global", None)
        record = markdown_store.read(expected_id)
        self.assertIn(short_evidence, record["summary"])

    def test_evidence_that_looks_like_a_tag_dump_is_omitted_not_truncated_mid_tag(self):
        log_like_evidence = (
            "<task-notification>background task finished successfully</task-notification>"
        )
        _build_fixture_db_with_long_evidence(self.db_path, log_like_evidence)

        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "primary_os", "global", None)
        record = markdown_store.read(expected_id)
        self.assertIsNotNone(record)
        self.assertNotIn("task-notification", record["summary"])
        self.assertIn("根拠", record["summary"])

    def test_evidence_that_looks_like_a_multiline_shell_transcript_is_omitted(self):
        log_like_evidence = (
            "user@host:~/project$ some-long-command --with-many-flags --verbose --output=json\n"
            "processing... this is a long log line that goes past the 80 character threshold used"
        )
        _build_fixture_db_with_long_evidence(self.db_path, log_like_evidence)

        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "primary_os", "global", None)
        record = markdown_store.read(expected_id)
        self.assertIsNotNone(record)
        self.assertNotIn("some-long-command", record["summary"])
        self.assertIn("根拠", record["summary"])


def _build_fixture_db_with_tied_valid_from(db_path: Path) -> None:
    """Fixture where two superseded rows for the same logical key share a
    ``valid_from`` timestamp but have different ``valid_until`` values, with
    the ``id`` sort order deliberately reversed relative to ``valid_until``.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT INTO memories VALUES('mem_shell_aaa','semantic','user','u1','preferred_shell',?,"
        "'好みのシェル: Zsh',0.6,0.6,'global',NULL,'superseded','2024-01-01T00:00:00+00:00',"
        "'2024-01-03T00:00:00+00:00','2024-01-01T00:00:00+00:00','2024-01-03T00:00:00+00:00')",
        (json.dumps({"value": "Zsh"}),),
    )
    conn.execute(
        "INSERT INTO memories VALUES('mem_shell_bbb','semantic','user','u1','preferred_shell',?,"
        "'好みのシェル: Bash',0.6,0.6,'global',NULL,'superseded','2024-01-01T00:00:00+00:00',"
        "'2024-01-02T00:00:00+00:00','2024-01-01T00:00:00+00:00','2024-01-02T00:00:00+00:00')",
        (json.dumps({"value": "Bash"}),),
    )
    conn.execute(
        "INSERT INTO memories VALUES('mem_shell_current','semantic','user','u1','preferred_shell',?,"
        "'好みのシェル: Fish',0.7,0.7,'global',NULL,'active','2024-01-05T00:00:00+00:00',NULL,"
        "'2024-01-01T00:00:00+00:00','2024-01-05T00:00:00+00:00')",
        (json.dumps({"value": "Fish"}),),
    )
    conn.commit()
    conn.close()


class TestMigrateHistoryOrdering(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.db_path = base / "memory.db"
        self.vault_dir = base / "vault"
        self.local_dir = base / "local"
        _build_fixture_db_with_tied_valid_from(self.db_path)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_apply_orders_history_rows_sharing_a_valid_from_by_valid_until_ascending(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "preferred_shell", "global", None)

        record = markdown_store.read(expected_id)
        self.assertIsNotNone(record)
        # Each history line documents one transition ("old → new"), so with
        # three chronological values (Bash, Zsh, Fish) there are two lines:
        # Bash->Zsh, then Zsh->Fish.
        self.assertEqual(len(record["history"]), 2)
        self.assertLess(
            record["history"][0].index("Bash"),
            record["history"][0].index("Zsh"),
            "the row with the earlier valid_until (Bash) must precede the row "
            "with the later valid_until (Zsh) within the first transition line, "
            "even though both rows share the same valid_from",
        )
        self.assertIn("Zsh", record["history"][1])
        self.assertIn("Fish", record["history"][1])


def _build_fixture_db_with_repeated_summary(db_path: Path) -> None:
    """Fixture where two consecutive superseded rows for the same logical key
    carry the exact same ``summary``/``value_json`` (e.g. a legacy row that
    was re-observed without its value actually changing)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT INTO memories VALUES('mem_lang_aaa','semantic','user','u1','preferred_language_runtime',?,"
        "'よく使う言語: TypeScript',0.6,0.6,'global',NULL,'superseded','2024-01-01T00:00:00+00:00',"
        "'2024-01-02T00:00:00+00:00','2024-01-01T00:00:00+00:00','2024-01-02T00:00:00+00:00')",
        (json.dumps({"value": "TypeScript"}),),
    )
    conn.execute(
        "INSERT INTO memories VALUES('mem_lang_bbb','semantic','user','u1','preferred_language_runtime',?,"
        "'よく使う言語: TypeScript',0.6,0.6,'global',NULL,'superseded','2024-01-02T00:00:00+00:00',"
        "'2024-01-03T00:00:00+00:00','2024-01-01T00:00:00+00:00','2024-01-03T00:00:00+00:00')",
        (json.dumps({"value": "TypeScript"}),),
    )
    conn.execute(
        "INSERT INTO memories VALUES('mem_lang_current','semantic','user','u1','preferred_language_runtime',?,"
        "'よく使う言語: Python',0.7,0.7,'global',NULL,'active','2024-01-03T00:00:00+00:00',NULL,"
        "'2024-01-01T00:00:00+00:00','2024-01-03T00:00:00+00:00')",
        (json.dumps({"value": "Python"}),),
    )
    conn.commit()
    conn.close()


class TestMigrateHistoryDeduplication(unittest.TestCase):
    """Rows whose summary is unchanged from the immediately preceding row do
    not generate a no-op ``X → X に変更`` history line."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.db_path = base / "memory.db"
        self.vault_dir = base / "vault"
        self.local_dir = base / "local"
        _build_fixture_db_with_repeated_summary(self.db_path)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_apply_skips_history_line_for_unchanged_adjacent_summary(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id(
            "user", "u1", "preferred_language_runtime", "global", None
        )

        record = markdown_store.read(expected_id)
        self.assertIsNotNone(record)
        # Three rows (TypeScript, TypeScript, Python) contain only one real
        # content change (TypeScript -> Python), so exactly one history line
        # is expected -- not two.
        self.assertEqual(len(record["history"]), 1)
        self.assertIn("TypeScript", record["history"][0])
        self.assertIn("Python", record["history"][0])


class TestMigrateIdempotency(MigrateTestBase):
    def test_rerunning_does_not_duplicate_events(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        local_store = LocalPipelineStore(self.local_dir)
        self.assertEqual(len(local_store.iter_events("sess_1")), 1)

    def test_rerunning_does_not_duplicate_observations(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        local_store = LocalPipelineStore(self.local_dir)
        self.assertEqual(len(local_store.iter_observations("sess_1")), 1)

    def test_rerunning_does_not_duplicate_memory_files(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        self.assertEqual(len(markdown_store.iter_all()), 1)

    def test_rerunning_does_not_duplicate_history_entries(self):
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)
        migrate(self.db_path, self.vault_dir, self.local_dir, apply=True)

        markdown_store = MarkdownMemoryStore(self.vault_dir)
        expected_id = canonical_memory_id("user", "u1", "preferred_editor", "global", None)
        record = markdown_store.read(expected_id)
        self.assertEqual(len(record["history"]), 1)


if __name__ == "__main__":
    unittest.main()
