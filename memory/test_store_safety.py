"""Storage boundaries and process coordination, using isolated files only."""

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_store import LocalPipelineStore
from markdown_store import MarkdownMemoryStore
from store_lock import lock_directory, store_lock


def update_memory(root, barrier, number):
    store = MarkdownMemoryStore(Path(root))
    barrier.wait(timeout=10)
    store.upsert_from_observation(
        type="feedback",
        entity_type="user",
        entity_id="default",
        key="concurrent",
        scope="global",
        project_id=None,
        summary=f"value-{number}",
    )


def hold_lock(root, ready, release):
    with store_lock(Path(root)):
        ready.set()
        release.wait(20)


def update_session(root, barrier, number):
    store = LocalPipelineStore(Path(root))
    barrier.wait(timeout=10)
    store.update_session("session", **{f"field_{number}": number})
    store.append_observation(
        session_id="session",
        source_event_id="event",
        entity_type="user",
        entity_id="default",
        attribute="key",
        value={"summary": "value"},
        confidence=0.8,
        scope="global",
        extractor_version="test",
    )


def write_different_memory(root, barrier, number):
    store = MarkdownMemoryStore(Path(root))
    barrier.wait(timeout=10)
    store.upsert_from_observation(
        type="feedback",
        entity_type="user",
        entity_id="default",
        key=f"key-{number}",
        scope="global",
        project_id=None,
        summary=f"value-{number}",
    )


def write_or_forget(root, barrier, number):
    store = MarkdownMemoryStore(Path(root))
    barrier.wait(timeout=10)
    if number == 0:
        store.forget("global/race")
    else:
        store.upsert_from_observation(
            type="feedback",
            entity_type="user",
            entity_id="default",
            key="race",
            scope="global",
            project_id=None,
            summary="after",
        )


class TestStoreBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cache = patch.dict(
            os.environ,
            {"XDG_CACHE_HOME": str(self.root / "cache"), "LOCALAPPDATA": str(self.root / "cache")},
        )
        cache.start()
        self.addCleanup(cache.stop)
        self.local = LocalPipelineStore(self.root / "local")
        self.vault = MarkdownMemoryStore(self.root / "vault")

    def test_session_ids_cannot_escape_directory(self):
        outside = self.local.local_dir / "outside.json"
        outside.write_text(json.dumps({"id": "../outside", "extracted_at": None}))
        original = outside.read_bytes()
        for invalid in ("../outside", "/tmp/outside", "a/b", "a\\b", "C:outside", "", ".", ".."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.local.mark_extracted(invalid)
        self.assertEqual(outside.read_bytes(), original)

    def test_internal_session_id_must_match_filename(self):
        path = self.local.sessions_dir / "safe.json"
        path.write_text(json.dumps({"id": "../outside", "extracted_at": None}))
        with self.assertRaises(ValueError):
            self.local.mark_extracted("safe")

    def test_memory_ids_reject_reserved_roots_and_windows_paths(self):
        for invalid in (
            "../outside",
            "archive/global/x",
            "_index",
            "unknown/x",
            "C:/x",
            "global/x:y",
            "global/../x",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.vault.forget(invalid)

    def test_linked_session_is_rejected_without_modifying_target(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"id": "safe", "extracted_at": None}))
        (self.local.sessions_dir / "safe.json").symlink_to(outside)
        original = outside.read_bytes()
        with self.assertRaises(ValueError):
            self.local.mark_extracted("safe")
        self.assertEqual(outside.read_bytes(), original)

    def test_conflicts_stop_mutation_and_preserve_original(self):
        record = self.vault.upsert_from_observation(
            type="feedback",
            entity_type="user",
            entity_id="default",
            key="x",
            scope="global",
            project_id=None,
            summary="original",
        )
        path = self.vault.memory_dir / "global/x.sync-conflict-test.md"
        path.write_text("conflict")
        with self.assertRaises(ValueError):
            self.vault.forget(record["id"])
        self.assertEqual(self.vault.read(record["id"])["summary"], "original")

    def test_concurrent_updates_preserve_every_value(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(5)
        processes = [
            context.Process(target=update_memory, args=(str(self.vault.vault_dir), barrier, i))
            for i in range(5)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        record = self.vault.read("global/concurrent")
        self.assertEqual(len(record["history"]), 4)
        combined = record["summary"] + "\n".join(record["history"])
        for i in range(5):
            self.assertIn(f"value-{i}", combined)

    def test_lock_timeout_and_release_after_process_exit(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=hold_lock, args=(str(self.vault.vault_dir), ready, release)
        )
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.assertRaises(TimeoutError):
                with self.vault.transaction(timeout=0.05):
                    pass
        finally:
            process.terminate()
            process.join(10)
        with self.vault.transaction(timeout=1):
            self.assertEqual(self.vault.iter_all(), [])

    def test_nested_instances_use_same_lock_without_deadlock(self):
        other = MarkdownMemoryStore(self.vault.vault_dir)
        with self.vault.transaction(timeout=1), other.transaction(timeout=1):
            self.assertEqual(other.iter_all(), [])
        self.assertFalse((self.vault.vault_dir / ".store.lock").exists())
        self.assertTrue(list(lock_directory().glob("*.lock")))

    def test_local_updates_and_dedup_are_atomic_across_processes(self):
        self.local.ensure_session("session", client="test", user_id="default", project_id=None)
        self._run_workers(update_session, self.local.local_dir)
        session = self.local.get_session("session")
        for number in range(5):
            self.assertEqual(session[f"field_{number}"], number)
        self.assertEqual(len(self.local.iter_observations("session")), 1)

    def test_concurrent_new_memories_are_all_in_the_index(self):
        self._run_workers(write_different_memory, self.vault.vault_dir)
        records = self.vault.iter_all()
        self.assertEqual(len(records), 5)
        index = (self.vault.memory_dir / "_index.md").read_text()
        for record in records:
            self.assertIn(f"({record['id']}.md)", index)

    def _run_workers(self, target, root):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(5)
        processes = [context.Process(target=target, args=(str(root), barrier, i)) for i in range(5)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(10)

    def test_linked_index_prevents_memory_mutation(self):
        outside = self.root / "outside.md"
        outside.write_text("untouched")
        (self.vault.memory_dir / "_index.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.vault.upsert_from_observation(
                type="feedback",
                entity_type="user",
                entity_id="default",
                key="x",
                scope="global",
                project_id=None,
                summary="new",
            )
        self.assertEqual(outside.read_text(), "untouched")
        self.assertFalse((self.vault.memory_dir / "global/x.md").exists())

    def test_rebuild_index_restores_index_without_changing_memory(self):
        record = self.vault.upsert_from_observation(
            type="feedback",
            entity_type="user",
            entity_id="default",
            key="x",
            scope="global",
            project_id=None,
            summary="original",
        )
        original = (self.vault.memory_dir / "global/x.md").read_bytes()
        index = self.vault.memory_dir / "_index.md"
        index.write_text("stale")
        self.vault.rebuild_index()
        self.assertIn(f"({record['id']}.md)", index.read_text())
        self.assertEqual((self.vault.memory_dir / "global/x.md").read_bytes(), original)

    def test_write_and_forget_preserve_both_values_and_consistent_index(self):
        self.vault.upsert_from_observation(
            type="feedback",
            entity_type="user",
            entity_id="default",
            key="race",
            scope="global",
            project_id=None,
            summary="before",
        )
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        processes = [
            context.Process(target=write_or_forget, args=(str(self.vault.vault_dir), barrier, i))
            for i in range(2)
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(10)
        archive = self.vault.memory_dir / "archive/global/race.md"
        self.assertTrue(archive.exists())
        contents = "\n".join(path.read_text() for path in self.vault.memory_dir.rglob("race.md"))
        self.assertIn("before", contents)
        self.assertIn("after", contents)
        index = (self.vault.memory_dir / "_index.md").read_text()
        self.assertEqual("(global/race.md)" in index, self.vault.read("global/race") is not None)

    def test_linked_memory_directory_and_archive_destination_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.vault.memory_dir / "global").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.vault.read("global/x")
        with self.assertRaises(ValueError):
            self.vault.upsert_from_observation(
                type="feedback",
                entity_type="user",
                entity_id="default",
                key="x",
                scope="global",
                project_id=None,
                summary="new",
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_index_and_conflict_names_are_not_valid_memory_ids(self):
        for invalid in ("global/_index", "global/x.sync-conflict-test"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.vault.read(invalid)
