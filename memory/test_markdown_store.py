#!/usr/bin/env python3
"""Tests for MarkdownMemoryStore (file-based memories layer, Syncthing-synced Vault)."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from markdown_store import (
    ARCHIVE_DIRNAME,
    INDEX_FILENAME,
    MarkdownMemoryStore,
    canonical_memory_id,
    format_history_date,
    humanize_key,
    render_history_line,
    resolve_vault_dir,
    slugify,
    today_date,
)


def _non_index_files(memory_dir: Path) -> list[Path]:
    return [p for p in memory_dir.glob("*.md") if p.name != INDEX_FILENAME]


def _all_memory_files(memory_dir: Path) -> list[Path]:
    return [
        p
        for p in memory_dir.rglob("*.md")
        if p.name != INDEX_FILENAME and ARCHIVE_DIRNAME not in p.relative_to(memory_dir).parts
    ]


class TestSlugify(unittest.TestCase):
    """slugify() produces a filesystem/link-safe kebab-case slug."""

    def test_converts_snake_case_to_kebab_case(self):
        self.assertEqual(slugify("preferred_editor"), "preferred-editor")

    def test_lowercases_input(self):
        self.assertEqual(slugify("MyProject"), "myproject")

    def test_collapses_invalid_characters_to_single_dash(self):
        self.assertEqual(slugify("foo / bar : baz"), "foo-bar-baz")

    def test_empty_input_falls_back_to_untitled(self):
        self.assertEqual(slugify("   "), "untitled")


class TestHumanizeKey(unittest.TestCase):
    """humanize_key() converts a snake_case key into a human-readable title."""

    def test_converts_snake_case_key_to_title(self):
        self.assertEqual(humanize_key("preferred_editor"), "Preferred Editor")

    def test_single_word_key_is_capitalized(self):
        self.assertEqual(humanize_key("summary"), "Summary")


class TestTodayDate(unittest.TestCase):
    """today_date() returns a date-only (no time-of-day) ISO string."""

    def test_returns_date_only_format(self):
        value = today_date()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")

    def test_uses_the_local_wall_clock_date_rather_than_utc(self):
        # A JST (UTC+9) user writing a memory at 08:30 local time on
        # 2026-01-02 is on the same UTC calendar day as 2026-01-01 23:30 UTC;
        # today_date() must report the local date (2026-01-02), not UTC's.
        local_now = datetime(2026, 1, 2, 8, 30)
        with patch("markdown_store.datetime") as mock_datetime:
            mock_datetime.now.return_value = local_now

            value = today_date()

        mock_datetime.now.assert_called_once_with()
        self.assertEqual(value, "2026-01-02")


class TestCanonicalMemoryId(unittest.TestCase):
    """canonical_memory_id() deterministically derives a human-readable slug."""

    def test_same_inputs_produce_same_id(self):
        first = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        second = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertEqual(first, second)

    def test_different_key_produces_different_id(self):
        first = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        second = canonical_memory_id("user", "default", "preferred_language_runtime", "global", None)
        self.assertNotEqual(first, second)

    def test_different_project_id_produces_different_id(self):
        first = canonical_memory_id("project", "p1", "recent_command", "project", "p1")
        second = canonical_memory_id("project", "p2", "recent_command", "project", "p2")
        self.assertNotEqual(first, second)

    def test_typical_user_default_entity_omits_entity_from_slug(self):
        generated = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertEqual(generated, "preferred-editor")

    def test_non_typical_entity_type_prefixes_entity_in_slug(self):
        generated = canonical_memory_id("project", "lab-web", "api_routing_design", "global", None)
        self.assertTrue(generated.startswith("project-lab-web-"))

    def test_non_typical_entity_id_prefixes_entity_in_slug(self):
        generated = canonical_memory_id("user", "someone-else", "preferred_editor", "global", None)
        self.assertTrue(generated.startswith("user-someone-else-"))

    def test_global_scope_has_no_directory_prefix(self):
        generated = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertNotIn("/", generated)


class TestCanonicalMemoryIdDirectoryLayout(unittest.TestCase):
    """canonical_memory_id() groups non-global scopes into their own subdirectory."""

    def test_project_scope_places_id_under_projects_directory(self):
        generated = canonical_memory_id(
            "user", "default", "db_migration_status", "project", "myproject"
        )
        self.assertEqual(generated, "projects/myproject/db-migration-status")

    def test_project_scope_with_non_typical_entity_prefixes_within_directory(self):
        generated = canonical_memory_id(
            "project", "lab-web", "api_routing_design", "project", "lab-web"
        )
        self.assertEqual(generated, "projects/lab-web/project-lab-web-api-routing-design")

    def test_project_scope_without_project_id_falls_back_to_flat_layout(self):
        generated = canonical_memory_id("user", "default", "recent_command", "project", None)
        self.assertNotIn("/", generated)

    def test_client_scope_places_id_under_clients_directory_grouped_by_entity(self):
        generated = canonical_memory_id("client", "acme-corp", "billing_note", "client", None)
        self.assertEqual(generated, "clients/acme-corp/client-acme-corp-billing-note")

    def test_temporary_scope_places_id_under_temporary_directory(self):
        generated = canonical_memory_id("user", "default", "scratch_note", "temporary", None)
        self.assertEqual(generated, "temporary/scratch-note")

    def test_same_logical_key_always_resolves_to_the_same_directory_and_slug(self):
        first = canonical_memory_id("user", "default", "db_migration_status", "project", "myproject")
        second = canonical_memory_id("user", "default", "db_migration_status", "project", "myproject")
        self.assertEqual(first, second)


class TestHistoryLineRendering(unittest.TestCase):
    """format_history_date()/render_history_line() build ``## 変更履歴`` lines."""

    def test_format_history_date_extracts_date_portion(self):
        self.assertEqual(format_history_date("2026-07-01T00:00:00+00:00"), "2026-07-01")

    def test_render_history_line_shows_old_and_new_value_and_date(self):
        line = render_history_line("Ubuntu を使用。", "Arch Linux を使用。", "2026-06-20")
        self.assertEqual(line, "2026-06-20: Ubuntu を使用。 → Arch Linux を使用。 に変更")

    def test_render_history_line_uses_only_the_first_line_of_multiline_summaries(self):
        old_summary = "コミットメッセージは詳細に書く\n\n**Why:** ...\n**How to apply:** ..."
        new_summary = "コミットメッセージは簡潔に書く\n\n**Why:** ...\n**How to apply:** ..."
        line = render_history_line(old_summary, new_summary, "2026-06-20")
        self.assertEqual(
            line, "2026-06-20: コミットメッセージは詳細に書く → コミットメッセージは簡潔に書く に変更"
        )


class TestResolveVaultDir(unittest.TestCase):
    """resolve_vault_dir() reads LLM_MEMORY_VAULT, falling back with a warning."""

    def test_uses_env_var_when_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"LLM_MEMORY_VAULT": tmpdir}):
                vault_dir, used_fallback = resolve_vault_dir(None)
            self.assertEqual(vault_dir, Path(tmpdir))
            self.assertFalse(used_fallback)

    def test_falls_back_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_MEMORY_VAULT", None)
            vault_dir, used_fallback = resolve_vault_dir(None)
        self.assertTrue(used_fallback)
        self.assertTrue(str(vault_dir).endswith("vault"))

    def test_explicit_argument_takes_priority_over_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            explicit = Path(tmpdir) / "explicit-vault"
            with patch.dict(os.environ, {"LLM_MEMORY_VAULT": "/should/not/be/used"}):
                vault_dir, used_fallback = resolve_vault_dir(explicit)
            self.assertEqual(vault_dir, explicit)
            self.assertFalse(used_fallback)


class MarkdownMemoryStoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmpdir.name) / "vault"
        self.store = MarkdownMemoryStore(self.vault_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _upsert(self, **overrides):
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
        return self.store.upsert_from_observation(**defaults)


class TestMarkdownMemoryStoreWriteRead(MarkdownMemoryStoreTestBase):
    """write()/read() round-trip preserves the minimal frontmatter and the summary body."""

    def test_memory_dir_is_created_under_vault(self):
        self.assertTrue((self.vault_dir / "memory").is_dir())

    def test_upsert_creates_a_markdown_file(self):
        self._upsert()
        files = _non_index_files(self.vault_dir / "memory")
        self.assertEqual(len(files), 1)

    def test_read_roundtrip_preserves_fields(self):
        created = self._upsert()
        fetched = self.store.read(created["id"])

        self.assertEqual(fetched["type"], "profile")
        self.assertEqual(fetched["scope"], "global")
        self.assertIsNone(fetched["project_id"])
        self.assertEqual(fetched["title"], "Preferred Editor")
        self.assertEqual(fetched["summary"], "好みのエディタ: Neovim")
        self.assertEqual(fetched["created"], created["created"])
        self.assertEqual(fetched["updated"], created["updated"])

    def test_frontmatter_only_contains_type_created_and_updated(self):
        created = self._upsert()
        path = self.store._path_for_id(created["id"])
        text = path.read_text(encoding="utf-8")
        frontmatter_text = text.split("---\n")[1]
        self.assertIn("type:", frontmatter_text)
        self.assertIn("created:", frontmatter_text)
        self.assertIn("updated:", frontmatter_text)
        for stale_field in (
            "id:",
            "memory_type:",
            "entity_type:",
            "entity_id:",
            "scope:",
            "project_id:",
            "confidence:",
            "salience:",
            "status:",
            "valid_from:",
            "valid_until:",
            "sources:",
            "value:",
        ):
            self.assertNotIn(stale_field, frontmatter_text)

    def test_write_uses_atomic_rename_no_temp_file_left_behind(self):
        self._upsert()
        leftover_tmp = list((self.vault_dir / "memory").glob("*.tmp*"))
        self.assertEqual(leftover_tmp, [])

    def test_read_returns_none_for_missing_id(self):
        result = self.store.read("does-not-exist")
        self.assertIsNone(result)

    def test_read_roundtrip_survives_embedded_horizontal_rule_in_summary(self):
        # A long string value containing a standalone "---" line (as produced by
        # markdown report text) must not be mistaken for the frontmatter delimiter,
        # even after PyYAML's automatic line-wrapping of long scalars.
        long_summary = (
            "line one of a long report. " * 20
            + "\n\n---\n\n# 見出し\n"
            + "line two of a long report. " * 20
        )
        created = self._upsert(summary=long_summary)

        fetched = self.store.read(created["id"])
        self.assertEqual(fetched["summary"], long_summary)

    def test_body_starts_with_humanized_key_heading(self):
        created = self._upsert()
        path = self.store._path_for_id(created["id"])
        text = path.read_text(encoding="utf-8")
        body = text.split("---\n", 2)[-1]
        self.assertIn("# Preferred Editor", body)

    def test_heading_is_not_included_in_parsed_summary(self):
        created = self._upsert()
        fetched = self.store.read(created["id"])
        self.assertNotIn("# Preferred Editor", fetched["summary"])


class TestMarkdownMemoryStoreFilenames(MarkdownMemoryStoreTestBase):
    """upsert_from_observation() names files after the key, not an opaque id."""

    def test_id_and_filename_stem_match(self):
        created = self._upsert()
        files = _non_index_files(self.vault_dir / "memory")
        self.assertEqual(files[0].stem, created["id"])

    def test_typical_entity_omits_entity_from_filename(self):
        created = self._upsert(entity_type="user", entity_id="default", scope="global")
        self.assertEqual(created["id"], "preferred-editor")

    def test_non_typical_entity_includes_entity_in_filename(self):
        created = self._upsert(
            key="api_routing_design",
            entity_type="project",
            entity_id="lab-web",
            scope="global",
            project_id=None,
            summary="APIルーティング方針: REST",
        )
        self.assertTrue(created["id"].startswith("project-lab-web-api-routing-design"))

    def test_colliding_slug_falls_back_to_numbered_suffix(self):
        # Pre-occupy the slug this upsert would otherwise deterministically pick,
        # simulating an unrelated pre-existing record with the same candidate slug.
        self.store.write(
            {
                "id": "preferred-editor",
                "type": "profile",
                "created": "2020-01-01",
                "updated": "2020-01-01",
                "title": "Unrelated Key",
                "summary": "unrelated",
                "history": [],
            }
        )

        created = self._upsert()
        self.assertEqual(created["id"], "preferred-editor-2")


class TestMarkdownMemoryStoreDirectoryLayout(MarkdownMemoryStoreTestBase):
    """upsert_from_observation() groups files into subdirectories by scope."""

    def test_global_scope_is_placed_directly_under_memory_dir(self):
        created = self._upsert(scope="global", project_id=None)

        path = self.store._path_for_id(created["id"])
        self.assertEqual(path.parent, self.vault_dir / "memory")

    def test_project_scope_is_placed_under_projects_subdirectory(self):
        created = self._upsert(
            key="recent_command",
            entity_type="project",
            entity_id="myproject",
            scope="project",
            project_id="myproject",
            summary="最近実行したコマンド: ls -la",
        )

        path = self.store._path_for_id(created["id"])
        self.assertEqual(path.parent, self.vault_dir / "memory" / "projects" / "myproject")
        self.assertTrue(path.exists())
        self.assertEqual(created["project_id"], "myproject")

    def test_project_scope_no_longer_uses_the_legacy_double_dash_suffix(self):
        created = self._upsert(
            key="recent_command",
            entity_type="project",
            entity_id="myproject",
            scope="project",
            project_id="myproject",
            summary="最近実行したコマンド: ls -la",
        )

        self.assertNotIn("--", created["id"])

    def test_client_scope_is_placed_under_clients_subdirectory(self):
        created = self._upsert(
            key="billing_note",
            entity_type="client",
            entity_id="acme-corp",
            scope="client",
            project_id=None,
            summary="請求条件: Net 30",
        )

        path = self.store._path_for_id(created["id"])
        self.assertEqual(path.parent, self.vault_dir / "memory" / "clients" / "acme-corp")
        self.assertTrue(path.exists())
        self.assertEqual(created["entity_id"], "acme-corp")

    def test_temporary_scope_is_placed_under_temporary_subdirectory(self):
        created = self._upsert(
            key="scratch_note",
            scope="temporary",
            project_id=None,
            summary="一時メモ: check CI status",
        )

        path = self.store._path_for_id(created["id"])
        self.assertEqual(path.parent, self.vault_dir / "memory" / "temporary")
        self.assertTrue(path.exists())

    def test_same_logical_key_always_resolves_to_the_same_file(self):
        first = self._upsert(
            key="recent_command",
            entity_type="project",
            entity_id="myproject",
            scope="project",
            project_id="myproject",
            summary="最近実行したコマンド: ls -la",
        )
        second = self._upsert(
            key="recent_command",
            entity_type="project",
            entity_id="myproject",
            scope="project",
            project_id="myproject",
            summary="最近実行したコマンド: git status",
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(_all_memory_files(self.vault_dir / "memory")), 1)


class TestMarkdownMemoryStoreUpsert(MarkdownMemoryStoreTestBase):
    """upsert_from_observation() folds content changes into the same file's history."""

    def test_first_write_uses_deterministic_canonical_id(self):
        created = self._upsert()
        expected_id = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertEqual(created["id"], expected_id)

    def test_same_summary_keeps_single_file_without_a_history_entry(self):
        first = self._upsert()
        second = self._upsert()

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["history"], [])
        files = _non_index_files(self.vault_dir / "memory")
        self.assertEqual(len(files), 1)

    def test_same_summary_and_type_does_not_bump_updated_date(self):
        with patch("markdown_store.today_date", return_value="2026-01-01"):
            first = self._upsert()
        with patch("markdown_store.today_date", return_value="2026-01-02"):
            second = self._upsert()

        self.assertEqual(first["updated"], "2026-01-01")
        self.assertEqual(second["updated"], "2026-01-01")

    def test_changed_type_with_same_summary_bumps_updated_date(self):
        with patch("markdown_store.today_date", return_value="2026-01-01"):
            self._upsert(type="profile")
        with patch("markdown_store.today_date", return_value="2026-01-02"):
            second = self._upsert(type="feedback")

        self.assertEqual(second["updated"], "2026-01-02")

    def test_different_summary_updates_the_same_file_in_place(self):
        self._upsert(summary="好みのエディタ: Neovim")
        self._upsert(summary="好みのエディタ: VSCode")

        active = self.store.search(type="profile")
        matching = [m for m in active if m["title"] == "Preferred Editor"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["summary"], "好みのエディタ: VSCode")

    def test_different_summary_appends_history_entry_without_creating_a_new_file(self):
        self._upsert(summary="好みのエディタ: Neovim")
        self._upsert(summary="好みのエディタ: VSCode")

        files = _non_index_files(self.vault_dir / "memory")
        self.assertEqual(len(files), 1)

        fetched = self.store.read(canonical_memory_id("user", "default", "preferred_editor", "global", None))
        self.assertEqual(fetched["summary"], "好みのエディタ: VSCode")
        self.assertEqual(len(fetched["history"]), 1)
        self.assertIn("Neovim", fetched["history"][0])

    def test_history_records_multiple_content_changes_in_chronological_order(self):
        self._upsert(summary="好みのエディタ: Neovim")
        self._upsert(summary="好みのエディタ: VSCode")
        third = self._upsert(summary="好みのエディタ: Emacs")

        self.assertEqual(len(third["history"]), 2)
        self.assertIn("Neovim", third["history"][0])
        self.assertIn("VSCode", third["history"][1])

    def test_history_survives_a_read_after_write_round_trip(self):
        self._upsert(summary="好みのエディタ: Neovim")
        created = self._upsert(summary="好みのエディタ: VSCode")

        fetched = self.store.read(created["id"])
        self.assertEqual(fetched["history"], created["history"])

    def test_created_date_is_preserved_across_content_changes(self):
        first = self._upsert(summary="好みのエディタ: Neovim")
        second = self._upsert(summary="好みのエディタ: VSCode")

        self.assertEqual(second["created"], first["created"])

    def test_new_record_after_content_change_reuses_canonical_id(self):
        self._upsert(summary="好みのエディタ: Neovim")
        second = self._upsert(summary="好みのエディタ: VSCode")

        expected_id = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertEqual(second["id"], expected_id)

    def test_type_can_change_when_content_is_recategorized(self):
        self._upsert(type="profile", summary="好みのエディタ: Neovim")
        second = self._upsert(type="feedback", summary="好みのエディタ: VSCode")

        self.assertEqual(second["type"], "feedback")

    def test_type_change_persists_even_when_summary_is_unchanged(self):
        self._upsert(type="profile", summary="好みのエディタ: Neovim")
        second = self._upsert(type="feedback", summary="好みのエディタ: Neovim")

        self.assertEqual(second["type"], "feedback")
        fetched = self.store.read(second["id"])
        self.assertEqual(fetched["type"], "feedback")


class TestMarkdownMemoryStoreProjectScopeValidation(MarkdownMemoryStoreTestBase):
    """upsert_from_observation() refuses to silently downgrade an unresolved
    project scope to a global-scope file."""

    def test_project_scope_without_project_id_raises_instead_of_falling_back_to_global(self):
        with self.assertRaises(ValueError):
            self._upsert(
                key="api_routing",
                entity_type="user",
                entity_id="default",
                scope="project",
                project_id=None,
                summary="APIルーティング方針: REST",
            )

    def test_project_scope_without_project_id_writes_no_file(self):
        with self.assertRaises(ValueError):
            self._upsert(
                key="api_routing",
                entity_type="user",
                entity_id="default",
                scope="project",
                project_id=None,
                summary="APIルーティング方針: REST",
            )

        self.assertEqual(_non_index_files(self.vault_dir / "memory"), [])


class TestMarkdownMemoryStoreForget(MarkdownMemoryStoreTestBase):
    """forget() archives a memory by moving its file, never deleting it."""

    def test_forget_moves_the_file_into_the_archive_directory(self):
        created = self._upsert()
        moved = self.store.forget(created["id"])

        self.assertEqual(moved, 1)
        archived_path = self.vault_dir / "memory" / ARCHIVE_DIRNAME / f"{created['id']}.md"
        self.assertTrue(archived_path.exists())

    def test_forget_removes_the_file_from_its_original_location(self):
        created = self._upsert()
        self.store.forget(created["id"])

        original_path = self.store._path_for_id(created["id"])
        self.assertFalse(original_path.exists())

    def test_forget_preserves_nested_relative_path_under_archive(self):
        created = self._upsert(
            key="recent_command",
            entity_type="project",
            entity_id="myproject",
            scope="project",
            project_id="myproject",
            summary="最近実行したコマンド: ls -la",
        )
        self.store.forget(created["id"])

        archived_path = self.vault_dir / "memory" / ARCHIVE_DIRNAME / f"{created['id']}.md"
        self.assertTrue(archived_path.exists())
        self.assertTrue(created["id"].startswith("projects/myproject/"))

    def test_forget_excludes_memory_from_search(self):
        created = self._upsert()
        self.store.forget(created["id"])

        results = self.store.search()
        ids = [m["id"] for m in results]
        self.assertNotIn(created["id"], ids)

    def test_forget_unknown_id_returns_zero(self):
        moved = self.store.forget("unknown-key")
        self.assertEqual(moved, 0)

    def test_forgetting_twice_returns_zero_the_second_time(self):
        created = self._upsert()
        self.store.forget(created["id"])
        second_attempt = self.store.forget(created["id"])
        self.assertEqual(second_attempt, 0)

    def test_a_new_memory_can_reclaim_the_canonical_slug_after_forget(self):
        created = self._upsert()
        self.store.forget(created["id"])

        recreated = self._upsert(summary="好みのエディタ: VSCode")
        self.assertEqual(recreated["id"], created["id"])

    def test_forgetting_a_reclaimed_slug_a_second_time_does_not_overwrite_the_first_archive(self):
        # A slug freed by forget() can be reclaimed by a new memory (see
        # test_a_new_memory_can_reclaim_the_canonical_slug_after_forget above).
        # When that reclaimed memory is later forgotten too, both archived
        # versions must remain on disk, distinguishable from each other.
        first = self._upsert(summary="OS: Arch Linux")
        self.store.forget(first["id"])
        first_archive_path = self.vault_dir / "memory" / ARCHIVE_DIRNAME / f"{first['id']}.md"
        first_archived_text = first_archive_path.read_text(encoding="utf-8")

        second = self._upsert(summary="OS: Ubuntu")
        self.assertEqual(second["id"], first["id"])
        self.store.forget(second["id"])

        self.assertTrue(first_archive_path.exists())
        self.assertEqual(first_archive_path.read_text(encoding="utf-8"), first_archived_text)
        self.assertIn("Arch Linux", first_archived_text)

        archive_dir = self.vault_dir / "memory" / ARCHIVE_DIRNAME
        archived_files = list(archive_dir.glob(f"{first['id']}*.md"))
        self.assertEqual(len(archived_files), 2)

        second_archive_path = next(p for p in archived_files if p != first_archive_path)
        self.assertIn("Ubuntu", second_archive_path.read_text(encoding="utf-8"))


class TestMarkdownMemoryStoreSearch(MarkdownMemoryStoreTestBase):
    """search() filters memories by type/scope/project/entity/query."""

    def test_search_filters_by_query_matching_title(self):
        self._upsert(key="preferred_editor", summary="好みのエディタ: Neovim")
        self._upsert(
            key="preferred_language_runtime",
            summary="よく使う言語: Python",
        )

        results = self.store.search(query="editor")
        titles = [m["title"] for m in results]
        self.assertIn("Preferred Editor", titles)
        self.assertNotIn("Preferred Language Runtime", titles)

    def test_search_filters_by_query_matching_summary(self):
        self._upsert(key="preferred_editor", summary="好みのエディタ: Neovim")

        results = self.store.search(query="neovim")
        self.assertEqual(len(results), 1)

    def test_search_filters_by_type(self):
        self._upsert(type="profile")
        self._upsert(
            key="commit_message_style",
            type="feedback",
            summary="コミットメッセージは簡潔に",
        )

        results = self.store.search(type="feedback")
        self.assertTrue(all(m["type"] == "feedback" for m in results))
        self.assertEqual(len(results), 1)

    def test_search_filters_by_scope(self):
        self._upsert(scope="global", project_id=None)
        self._upsert(
            key="recent_command",
            scope="project",
            project_id="proj1",
            entity_type="project",
            entity_id="proj1",
            summary="最近実行したコマンド: ls -la",
        )

        results = self.store.search(scope="project")
        self.assertTrue(all(m["scope"] == "project" for m in results))

    def test_search_filters_by_project_id(self):
        self._upsert(
            key="recent_command",
            scope="project",
            project_id="proj1",
            entity_type="project",
            entity_id="proj1",
            summary="最近実行したコマンド: ls -la",
        )

        results = self.store.search(project_id="proj1")
        self.assertTrue(all(m["project_id"] == "proj1" for m in results))

    def test_search_filters_by_client_entity_id(self):
        self._upsert(
            key="billing_note",
            entity_type="client",
            entity_id="acme-corp",
            scope="client",
            project_id=None,
            summary="請求条件: Net 30",
        )

        results = self.store.search(entity_id="acme-corp")
        self.assertEqual(len(results), 1)

    def test_search_excludes_archived_memories(self):
        created = self._upsert()
        self.store.forget(created["id"])

        results = self.store.search()
        self.assertEqual(results, [])


class TestMarkdownMemoryStoreGetContext(MarkdownMemoryStoreTestBase):
    """get_context() returns global memories plus the current project's memories."""

    def test_returns_global_scoped_memories(self):
        self._upsert(scope="global")

        results = self.store.get_context(project_id="my-project")
        self.assertEqual(len(results), 1)

    def test_returns_project_scoped_memories(self):
        self._upsert(
            key="recent_command",
            scope="project",
            project_id="my-project",
            entity_type="project",
            entity_id="my-project",
            summary="最近実行したコマンド: ls -la",
        )

        results = self.store.get_context(project_id="my-project")
        self.assertEqual(len(results), 1)

    def test_excludes_other_projects_scoped_memories(self):
        self._upsert(
            key="recent_command",
            scope="project",
            project_id="other-project",
            entity_type="project",
            entity_id="other-project",
            summary="最近実行したコマンド: ls -la",
        )

        results = self.store.get_context(project_id="my-project")
        self.assertEqual(results, [])


class TestMarkdownMemoryStoreDelete(MarkdownMemoryStoreTestBase):
    """delete() physically removes a memory file (used by the cleanup subcommand only)."""

    def test_delete_removes_the_file(self):
        created = self._upsert()
        removed = self.store.delete(created["id"])

        self.assertTrue(removed)
        self.assertIsNone(self.store.read(created["id"]))

    def test_delete_unknown_id_returns_false(self):
        removed = self.store.delete("unknown-key")
        self.assertFalse(removed)


class TestMarkdownMemoryStoreSyncConflictDetection(MarkdownMemoryStoreTestBase):
    """iter_all() warns about Syncthing conflict files and excludes them from results."""

    def test_warns_on_stderr_when_conflict_file_present(self):
        self._upsert()
        memory_dir = self.vault_dir / "memory"
        conflict_file = memory_dir / "preferred-editor.sync-conflict-20260101-000000-ABCDEFG.md"
        conflict_file.write_text("---\ntype: profile\n---\nconflicted", encoding="utf-8")

        buffer = io.StringIO()
        with redirect_stderr(buffer):
            self.store.iter_all()

        self.assertIn("sync-conflict", buffer.getvalue())

    def test_conflict_files_are_excluded_from_results(self):
        created = self._upsert()
        memory_dir = self.vault_dir / "memory"
        conflict_file = memory_dir / f"{created['id']}.sync-conflict-20260101-000000-ABCDEFG.md"
        conflict_file.write_text("---\ntype: profile\n---\nconflicted", encoding="utf-8")

        results = self.store.iter_all()
        ids = [m["id"] for m in results]
        self.assertEqual(ids.count(created["id"]), 1)


class TestMarkdownMemoryStoreIndex(MarkdownMemoryStoreTestBase):
    """The store maintains a generated `_index.md` listing non-archived memories."""

    def test_index_file_is_created_after_upsert(self):
        self._upsert()
        index_path = self.vault_dir / "memory" / INDEX_FILENAME
        self.assertTrue(index_path.exists())

    def test_index_lists_memory_with_link_and_summary(self):
        created = self._upsert()
        index_text = (self.vault_dir / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")

        self.assertIn(f"({created['id']}.md)", index_text)
        self.assertIn("Preferred Editor", index_text)
        self.assertIn("好みのエディタ: Neovim", index_text)

    def test_index_groups_project_scoped_memories_under_their_own_section(self):
        self._upsert(scope="global")
        self._upsert(
            key="recent_command",
            scope="project",
            project_id="myproject",
            entity_type="project",
            entity_id="myproject",
            summary="最近実行したコマンド: ls -la",
        )

        index_text = (self.vault_dir / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("## Global", index_text)
        self.assertIn("## Project: myproject", index_text)

    def test_forgotten_memory_is_removed_from_index(self):
        created = self._upsert()
        self.store.forget(created["id"])

        index_text = (self.vault_dir / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(f"({created['id']}.md)", index_text)

    def test_index_file_is_not_treated_as_a_memory_record(self):
        self._upsert()
        records = self.store.iter_all()
        ids = [r["id"] for r in records]
        self.assertNotIn("_index", ids)
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
