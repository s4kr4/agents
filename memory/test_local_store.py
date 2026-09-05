#!/usr/bin/env python3
"""Tests for LocalPipelineStore (non-Vault, per-machine sessions/events/observations/logs)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_store import LocalPipelineStore, resolve_local_dir


class TestResolveLocalDir(unittest.TestCase):
    """resolve_local_dir() reads LLM_MEMORY_LOCAL_DIR, defaulting to memory/local."""

    def test_uses_env_var_when_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"LLM_MEMORY_LOCAL_DIR": tmpdir}):
                local_dir = resolve_local_dir(None)
            self.assertEqual(local_dir, Path(tmpdir))

    def test_defaults_to_memory_local(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_MEMORY_LOCAL_DIR", None)
            local_dir = resolve_local_dir(None)
        self.assertTrue(str(local_dir).endswith(str(Path("memory") / "local")))

    def test_explicit_argument_takes_priority_over_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            explicit = Path(tmpdir) / "explicit-local"
            with patch.dict(os.environ, {"LLM_MEMORY_LOCAL_DIR": "/should/not/be/used"}):
                local_dir = resolve_local_dir(explicit)
            self.assertEqual(local_dir, explicit)


class LocalPipelineStoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.local_dir = Path(self._tmpdir.name) / "local"
        self.store = LocalPipelineStore(self.local_dir)

    def tearDown(self):
        self._tmpdir.cleanup()


class TestLocalPipelineStoreDirectoryLayout(LocalPipelineStoreTestBase):
    def test_creates_expected_subdirectories(self):
        self.assertTrue((self.local_dir / "sessions").is_dir())
        self.assertTrue((self.local_dir / "events").is_dir())
        self.assertTrue((self.local_dir / "observations").is_dir())
        self.assertTrue((self.local_dir / "logs").is_dir())


class TestLocalPipelineStoreSessions(LocalPipelineStoreTestBase):
    """ensure_session()/get_session()/update_session() manage sessions/<id>.json."""

    def test_ensure_session_creates_session_file(self):
        self.store.ensure_session(
            "sess_1", client="claude-code", user_id="default", project_id="proj"
        )

        path = self.local_dir / "sessions" / "sess_1.json"
        self.assertTrue(path.exists())

    def test_ensure_session_is_idempotent(self):
        self.store.ensure_session(
            "sess_1", client="claude-code", user_id="default", project_id="proj"
        )
        self.store.update_session("sess_1", summary="first summary")
        self.store.ensure_session(
            "sess_1", client="claude-code", user_id="default", project_id="proj"
        )

        session = self.store.get_session("sess_1")
        self.assertEqual(session["summary"], "first summary")

    def test_get_session_returns_none_when_missing(self):
        self.assertIsNone(self.store.get_session("sess_missing"))

    def test_write_session_stores_full_record_atomically(self):
        session = {
            "id": "sess_migrated",
            "client": "codex",
            "user_id": "u1",
            "project_id": "p1",
            "started_at": "2024-01-01T00:00:00+00:00",
            "ended_at": "2024-01-01T00:05:00+00:00",
            "summary": "a summary",
            "extracted_at": "2024-01-01T00:06:00+00:00",
        }
        self.store.write_session(session)

        fetched = self.store.get_session("sess_migrated")
        self.assertEqual(fetched, session)

    def test_write_session_overwrites_existing_record(self):
        self.store.ensure_session("sess_1", client="c", user_id="u1", project_id=None)
        self.store.write_session(
            {
                "id": "sess_1",
                "client": "c",
                "user_id": "u1",
                "project_id": None,
                "started_at": "2024-01-01T00:00:00+00:00",
                "ended_at": None,
                "summary": "overwritten",
                "extracted_at": None,
            }
        )

        fetched = self.store.get_session("sess_1")
        self.assertEqual(fetched["summary"], "overwritten")

    def test_update_session_merges_fields(self):
        self.store.ensure_session(
            "sess_1", client="claude-code", user_id="default", project_id="proj"
        )
        self.store.update_session("sess_1", ended_at="2026-01-01T00:00:00+00:00", summary="done")

        session = self.store.get_session("sess_1")
        self.assertEqual(session["ended_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(session["summary"], "done")
        self.assertEqual(session["client"], "claude-code")

    def test_list_unextracted_returns_sessions_with_summary_and_no_extracted_at(self):
        self.store.ensure_session("sess_a", client="c", user_id="u1", project_id=None)
        self.store.update_session("sess_a", summary="has summary")

        self.store.ensure_session("sess_b", client="c", user_id="u1", project_id=None)
        # no summary set

        self.store.ensure_session("sess_c", client="c", user_id="u1", project_id=None)
        self.store.update_session(
            "sess_c", summary="already extracted", extracted_at="2026-01-01T00:00:00+00:00"
        )

        results = self.store.list_unextracted()
        ids = [s["id"] for s in results]
        self.assertIn("sess_a", ids)
        self.assertNotIn("sess_b", ids)
        self.assertNotIn("sess_c", ids)

    def test_list_unextracted_respects_limit(self):
        for i in range(5):
            session_id = f"sess_{i}"
            self.store.ensure_session(session_id, client="c", user_id="u1", project_id=None)
            self.store.update_session(session_id, summary=f"summary {i}")

        results = self.store.list_unextracted(limit=3)
        self.assertEqual(len(results), 3)

    def test_mark_extracted_sets_extracted_at(self):
        self.store.ensure_session("sess_1", client="c", user_id="u1", project_id=None)
        self.store.update_session("sess_1", summary="s")

        updated = self.store.mark_extracted("sess_1")
        self.assertEqual(updated, 1)

        session = self.store.get_session("sess_1")
        self.assertIsNotNone(session["extracted_at"])

    def test_mark_extracted_already_extracted_returns_zero(self):
        self.store.ensure_session("sess_1", client="c", user_id="u1", project_id=None)
        self.store.mark_extracted("sess_1")

        updated = self.store.mark_extracted("sess_1")
        self.assertEqual(updated, 0)

    def test_mark_extracted_unknown_session_returns_zero(self):
        updated = self.store.mark_extracted("sess_missing")
        self.assertEqual(updated, 0)


class TestLocalPipelineStoreEvents(LocalPipelineStoreTestBase):
    """append_event()/iter_events() manage events/<session_id>.jsonl."""

    def test_append_event_creates_jsonl_file(self):
        self.store.append_event(
            "sess_1", role="user", kind="message", content="hello", importance=0.5
        )

        path = self.local_dir / "events" / "sess_1.jsonl"
        self.assertTrue(path.exists())

    def test_append_event_appends_one_line_per_call(self):
        self.store.append_event("sess_1", role="user", kind="message", content="a", importance=0.5)
        self.store.append_event(
            "sess_1", role="assistant", kind="message", content="b", importance=0.5
        )

        events = self.store.iter_events("sess_1")
        self.assertEqual(len(events), 2)

    def test_append_event_returns_event_with_generated_id(self):
        event = self.store.append_event(
            "sess_1", role="user", kind="message", content="hi", importance=0.5
        )
        self.assertIn("id", event)
        self.assertEqual(event["session_id"], "sess_1")

    def test_iter_events_returns_empty_list_for_unknown_session(self):
        self.assertEqual(self.store.iter_events("sess_missing"), [])

    def test_iter_all_events_spans_multiple_sessions(self):
        self.store.append_event("sess_1", role="user", kind="message", content="a", importance=0.5)
        self.store.append_event("sess_2", role="user", kind="message", content="b", importance=0.5)

        all_events = self.store.iter_all_events()
        self.assertEqual(len(all_events), 2)


class TestLocalPipelineStoreObservations(LocalPipelineStoreTestBase):
    """append_observation()/iter_observations() manage observations/<session_id>.jsonl with dedup."""

    def _make_observation(self, **overrides):
        defaults = {
            "session_id": "sess_1",
            "source_event_id": "evt_1",
            "entity_type": "user",
            "entity_id": "default",
            "attribute": "preferred_editor",
            "value": {"memory_type": "semantic", "value": "Neovim"},
            "confidence": 0.7,
            "scope": "global",
            "extractor_version": "rule-based-v1",
        }
        defaults.update(overrides)
        return defaults

    def test_append_observation_creates_file(self):
        self.store.append_observation(**self._make_observation())

        path = self.local_dir / "observations" / "sess_1.jsonl"
        self.assertTrue(path.exists())

    def test_append_observation_deduplicates_identical_keys(self):
        first = self.store.append_observation(**self._make_observation())
        second = self.store.append_observation(**self._make_observation())

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.store.iter_observations("sess_1")), 1)

    def test_append_observation_allows_different_attribute(self):
        self.store.append_observation(**self._make_observation())
        self.store.append_observation(**self._make_observation(attribute="primary_os"))

        self.assertEqual(len(self.store.iter_observations("sess_1")), 2)

    def test_append_observation_stores_explicit_project_id(self):
        observation = self.store.append_observation(
            **self._make_observation(scope="project", project_id="lab-web")
        )

        self.assertEqual(observation["project_id"], "lab-web")
        stored = self.store.iter_observations("sess_1")[0]
        self.assertEqual(stored["project_id"], "lab-web")

    def test_append_observation_defaults_project_id_to_none(self):
        observation = self.store.append_observation(**self._make_observation())

        self.assertIsNone(observation["project_id"])

    def test_iter_all_observations_spans_multiple_sessions(self):
        self.store.append_observation(**self._make_observation(session_id="sess_1"))
        self.store.append_observation(
            **self._make_observation(session_id="sess_2", source_event_id="evt_2")
        )

        self.assertEqual(len(self.store.iter_all_observations()), 2)

    def test_remove_matching_observations_deletes_by_attribute(self):
        self.store.append_observation(**self._make_observation(attribute="recent_summary"))
        self.store.append_observation(
            **self._make_observation(attribute="preferred_editor", source_event_id="evt_2")
        )

        removed = self.store.remove_matching_observations("sess_1", attribute="recent_summary")

        self.assertEqual(removed, 1)
        remaining = self.store.iter_observations("sess_1")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["attribute"], "preferred_editor")

    def test_remove_matching_observations_returns_zero_when_no_match(self):
        self.store.append_observation(**self._make_observation(attribute="preferred_editor"))

        removed = self.store.remove_matching_observations("sess_1", attribute="recent_summary")
        self.assertEqual(removed, 0)

    def test_remove_matching_observations_unknown_session_returns_zero(self):
        removed = self.store.remove_matching_observations(
            "sess_missing", attribute="recent_summary"
        )
        self.assertEqual(removed, 0)


class TestLocalPipelineStoreLogs(LocalPipelineStoreTestBase):
    """append_retrieval_log()/append_deletion_log() write to logs/*.jsonl."""

    def test_append_retrieval_log_appends_entry(self):
        self.store.append_retrieval_log(
            session_id="sess_1", query="editor", returned_memory_ids=["mem_1"]
        )

        path = self.local_dir / "logs" / "retrieval.jsonl"
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_append_deletion_log_appends_entry(self):
        self.store.append_deletion_log(target_type="memory", target_id="mem_1", reason="stale")

        path = self.local_dir / "logs" / "deletions.jsonl"
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)


class TestLocalPipelineStoreVaultIsolation(unittest.TestCase):
    """Pipeline files must never be written inside a Vault directory."""

    def test_local_dir_is_independent_of_vault_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = Path(tmpdir) / "vault"
            local_dir = Path(tmpdir) / "local"
            store = LocalPipelineStore(local_dir)
            store.ensure_session("sess_1", client="c", user_id="u1", project_id=None)

            self.assertFalse(vault_dir.exists())
            self.assertTrue((local_dir / "sessions" / "sess_1.json").exists())


if __name__ == "__main__":
    unittest.main()
