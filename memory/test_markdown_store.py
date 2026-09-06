#!/usr/bin/env python3
"""Tests for MarkdownMemoryStore (file-based memories layer, Syncthing-synced Vault)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from markdown_store import (
    ARCHIVE_DIRNAME,
    INDEX_FILENAME,
    MarkdownMemoryStore,
    canonical_memory_id,
    current_timestamp,
    format_history_date,
    humanize_key,
    normalize_related,
    normalize_tag,
    normalize_tags,
    render_history_line,
    resolve_vault_dir,
    slugify,
)
from store_paths import StorePathError


def _non_index_files(memory_dir: Path) -> list[Path]:
    return [
        p
        for p in memory_dir.rglob("*.md")
        if p.name != INDEX_FILENAME and ARCHIVE_DIRNAME not in p.relative_to(memory_dir).parts
    ]


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


class TestCurrentTimestamp(unittest.TestCase):
    def test_returns_seconds_and_timezone(self):
        self.assertRegex(
            current_timestamp(),
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
        )

    def test_preserves_local_time_and_offset(self):
        local_now = datetime(2026, 1, 2, 8, 30, 45, 123456, timezone(timedelta(hours=9)))
        with patch("markdown_store.datetime") as mock_datetime:
            mock_datetime.now.return_value.astimezone.return_value = local_now
            value = current_timestamp()
        self.assertEqual(value, "2026-01-02T08:30:45+09:00")


class TestCanonicalMemoryId(unittest.TestCase):
    """canonical_memory_id() deterministically derives a human-readable slug."""

    def test_same_inputs_produce_same_id(self):
        first = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        second = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertEqual(first, second)

    def test_different_key_produces_different_id(self):
        first = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        second = canonical_memory_id(
            "user", "default", "preferred_language_runtime", "global", None
        )
        self.assertNotEqual(first, second)

    def test_different_project_id_produces_different_id(self):
        first = canonical_memory_id("project", "p1", "recent_command", "project", "p1")
        second = canonical_memory_id("project", "p2", "recent_command", "project", "p2")
        self.assertNotEqual(first, second)

    def test_typical_user_default_entity_omits_entity_from_slug(self):
        generated = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertEqual(generated, "global/preferred-editor")

    def test_non_typical_entity_type_prefixes_entity_in_slug(self):
        generated = canonical_memory_id("project", "lab-web", "api_routing_design", "global", None)
        self.assertTrue(generated.startswith("global/project-lab-web-"))

    def test_non_typical_entity_id_prefixes_entity_in_slug(self):
        generated = canonical_memory_id("user", "someone-else", "preferred_editor", "global", None)
        self.assertTrue(generated.startswith("global/user-someone-else-"))

    def test_global_scope_is_placed_under_global_directory(self):
        generated = canonical_memory_id("user", "default", "preferred_editor", "global", None)
        self.assertEqual(generated, "global/preferred-editor")


class TestCanonicalMemoryIdDirectoryLayout(unittest.TestCase):
    """canonical_memory_id() always groups memories into a scope subdirectory."""

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
        first = canonical_memory_id(
            "user", "default", "db_migration_status", "project", "myproject"
        )
        second = canonical_memory_id(
            "user", "default", "db_migration_status", "project", "myproject"
        )
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
            line,
            "2026-06-20: コミットメッセージは詳細に書く → コミットメッセージは簡潔に書く に変更",
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

    def test_creation_and_same_day_update_persist_timestamps(self):
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T08:30:00+09:00"):
            first = self._upsert()
        self.assertEqual(first["created"], "2026-01-01T08:30:00+09:00")
        self.assertEqual(first["updated"], first["created"])
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T09:30:00+09:00"):
            second = self._upsert(summary="好みのエディタ: VSCode")
        fetched = self.store.read(second["id"])
        self.assertEqual(fetched["created"], first["created"])
        self.assertEqual(fetched["updated"], "2026-01-01T09:30:00+09:00")

    def test_updating_legacy_record_preserves_unknown_creation_time(self):
        first = self._upsert()
        first["created"] = first["updated"] = "2026-01-01"
        self.store.write(first)
        with patch("markdown_store.current_timestamp", return_value="2026-01-02T09:30:00+09:00"):
            second = self._upsert(summary="好みのエディタ: VSCode")
        fetched = self.store.read(second["id"])
        self.assertEqual(fetched["created"], "2026-01-01")
        self.assertEqual(fetched["updated"], "2026-01-02T09:30:00+09:00")

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

    def test_read_normalizes_unquoted_yaml_dates_to_strings(self):
        path = self.vault_dir / "memory" / "global" / "legacy-memory.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ntype: profile\ncreated: 2026-01-01\nupdated: 2026-08-06\n---\n\n"
            "# Legacy Memory\n\n過去形式のメモリ\n",
            encoding="utf-8",
        )

        fetched = self.store.read("global/legacy-memory")

        self.assertEqual(fetched["created"], "2026-01-01")
        self.assertEqual(fetched["updated"], "2026-08-06")

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
        result = self.store.read("global/does-not-exist")
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
        self.assertEqual(
            files[0].relative_to(self.vault_dir / "memory").with_suffix("").as_posix(),
            created["id"],
        )

    def test_typical_entity_omits_entity_from_filename(self):
        created = self._upsert(entity_type="user", entity_id="default", scope="global")
        self.assertEqual(created["id"], "global/preferred-editor")

    def test_non_typical_entity_includes_entity_in_filename(self):
        created = self._upsert(
            key="api_routing_design",
            entity_type="project",
            entity_id="lab-web",
            scope="global",
            project_id=None,
            summary="APIルーティング方針: REST",
        )
        self.assertTrue(created["id"].startswith("global/project-lab-web-api-routing-design"))

    def test_colliding_slug_falls_back_to_numbered_suffix(self):
        # Pre-occupy the slug this upsert would otherwise deterministically pick,
        # simulating an unrelated pre-existing record with the same candidate slug.
        self.store.write(
            {
                "id": "global/preferred-editor",
                "type": "profile",
                "created": "2020-01-01",
                "updated": "2020-01-01",
                "title": "Unrelated Key",
                "summary": "unrelated",
                "history": [],
            }
        )

        created = self._upsert()
        self.assertEqual(created["id"], "global/preferred-editor-2")


class TestMarkdownMemoryStoreDirectoryLayout(MarkdownMemoryStoreTestBase):
    """upsert_from_observation() groups files into subdirectories by scope."""

    def test_global_scope_is_placed_under_global_subdirectory(self):
        created = self._upsert(scope="global", project_id=None)

        path = self.store._path_for_id(created["id"])
        self.assertEqual(path.parent, self.vault_dir / "memory" / "global")

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


class TestLegacyRootLayoutMigration(MarkdownMemoryStoreTestBase):
    def test_moves_legacy_root_memory_into_global_directory_and_refreshes_index(self):
        legacy_path = self.vault_dir / "memory" / "preferred-editor.md"
        # Construct the pre-layout fixture without using the active-memory API.
        legacy_path.write_text(
            "---\ntype: profile\ncreated: 2026-08-06\nupdated: 2026-08-06\n---\n\n"
            "# Preferred Editor\n\n好みのエディタ: Neovim\n",
            encoding="utf-8",
        )

        moved = self.store.migrate_legacy_root_memories()

        self.assertEqual(moved, ["global/preferred-editor"])
        self.assertFalse(legacy_path.exists())
        self.assertTrue((self.vault_dir / "memory" / "global" / "preferred-editor.md").exists())
        self.assertIn(
            "global/preferred-editor.md", (self.vault_dir / "memory" / INDEX_FILENAME).read_text()
        )
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
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T08:30:00+09:00"):
            first = self._upsert()
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T09:30:00+09:00"):
            second = self._upsert()

        self.assertEqual(first["updated"], "2026-01-01T08:30:00+09:00")
        self.assertEqual(second["updated"], "2026-01-01T08:30:00+09:00")

    def test_changed_type_with_same_summary_bumps_updated_date(self):
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T08:30:00+09:00"):
            self._upsert(type="profile")
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T09:30:00+09:00"):
            second = self._upsert(type="feedback")

        self.assertEqual(second["updated"], "2026-01-01T09:30:00+09:00")

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

        fetched = self.store.read(
            canonical_memory_id("user", "default", "preferred_editor", "global", None)
        )
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
        moved = self.store.forget("global/unknown-key")
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
        removed = self.store.delete("global/unknown-key")
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


class TestNormalizeTag(unittest.TestCase):
    """normalize_tag() lowercases, kebab-cases, and preserves Unicode/hierarchy."""

    def test_lowercases_and_trims_whitespace(self):
        self.assertEqual(normalize_tag("  Docker  "), "docker")

    def test_converts_underscore_and_whitespace_to_dash(self):
        self.assertEqual(normalize_tag("my tag_name"), "my-tag-name")

    def test_collapses_invalid_characters_to_single_dash(self):
        self.assertEqual(normalize_tag("foo!!!bar"), "foo-bar")

    def test_collapses_consecutive_dashes(self):
        self.assertEqual(normalize_tag("foo---bar"), "foo-bar")

    def test_strips_leading_and_trailing_dashes(self):
        self.assertEqual(normalize_tag("-foo-"), "foo")

    def test_preserves_japanese_characters(self):
        self.assertEqual(normalize_tag("日本語"), "日本語")

    def test_preserves_obsidian_hierarchical_slash(self):
        self.assertEqual(normalize_tag("env/wsl"), "env/wsl")

    def test_raises_for_non_string_input(self):
        with self.assertRaises(ValueError):
            normalize_tag(123)  # type: ignore[arg-type]

    def test_raises_for_empty_string(self):
        with self.assertRaises(ValueError):
            normalize_tag("")

    def test_raises_when_normalized_result_is_empty(self):
        with self.assertRaises(ValueError):
            normalize_tag("!!!")

    def test_cpp_and_csharp_both_normalize_to_c_known_limitation(self):
        self.assertEqual(normalize_tag("C++"), normalize_tag("C#"))


class TestNormalizeTags(unittest.TestCase):
    """normalize_tags() validates a list, normalizes, dedupes and sorts."""

    def test_normalizes_each_element(self):
        self.assertEqual(normalize_tags(["Docker", "GPU"]), ["docker", "gpu"])

    def test_deduplicates_after_normalization(self):
        self.assertEqual(normalize_tags(["docker", "Docker", "DOCKER"]), ["docker"])

    def test_sorts_result_deterministically(self):
        self.assertEqual(normalize_tags(["gpu", "docker", "linux"]), ["docker", "gpu", "linux"])

    def test_empty_list_is_valid_and_returns_empty_list(self):
        self.assertEqual(normalize_tags([]), [])

    def test_raises_for_non_list_input(self):
        with self.assertRaises(ValueError):
            normalize_tags("docker")

    def test_raises_for_non_string_element(self):
        with self.assertRaises(ValueError):
            normalize_tags(["docker", 123])

    def test_raises_for_empty_string_element(self):
        with self.assertRaises(ValueError):
            normalize_tags(["docker", ""])

    def test_raises_for_element_that_normalizes_to_empty(self):
        with self.assertRaises(ValueError):
            normalize_tags(["docker", "!!!"])


class TestNormalizeRelated(unittest.TestCase):
    """normalize_related() validates id shape, removes self-refs/dupes, sorts."""

    def test_validates_and_returns_sorted_ids(self):
        self.assertEqual(
            normalize_related(["global/b", "global/a"]),
            ["global/a", "global/b"],
        )

    def test_deduplicates_ids(self):
        self.assertEqual(normalize_related(["global/a", "global/a"]), ["global/a"])

    def test_removes_self_reference(self):
        self.assertEqual(
            normalize_related(["global/a", "global/self"], self_id="global/self"),
            ["global/a"],
        )

    def test_empty_list_is_valid(self):
        self.assertEqual(normalize_related([]), [])

    def test_raises_for_non_list_input(self):
        with self.assertRaises(ValueError):
            normalize_related("global/a")

    def test_raises_for_non_string_element(self):
        with self.assertRaises(ValueError):
            normalize_related([123])

    def test_raises_for_bare_slug_without_directory(self):
        with self.assertRaises(StorePathError):
            normalize_related(["bare-slug"])

    def test_raises_for_index_id(self):
        with self.assertRaises(StorePathError):
            normalize_related(["global/_index"])

    def test_does_not_check_existence(self):
        # Existence is a read-time concern (see related()), not a write-time one.
        self.assertEqual(normalize_related(["global/does-not-exist"]), ["global/does-not-exist"])


class TestMarkdownMemoryStoreTagsRelatedRoundTrip(MarkdownMemoryStoreTestBase):
    """tags/related survive a write/read round trip via upsert_from_observation()."""

    def test_tags_round_trip(self):
        created = self._upsert(tags=["Docker", "gpu"])
        fetched = self.store.read(created["id"])
        self.assertEqual(fetched["tags"], ["docker", "gpu"])

    def test_japanese_tag_round_trip_is_not_corrupted(self):
        created = self._upsert(tags=["日本語"])
        fetched = self.store.read(created["id"])
        self.assertEqual(fetched["tags"], ["日本語"])
        path = self.store._path_for_id(created["id"])
        self.assertIn("日本語", path.read_text(encoding="utf-8"))

    def test_related_round_trip(self):
        other = self._upsert(key="other_key", summary="other")
        created = self._upsert(related=[other["id"]])
        fetched = self.store.read(created["id"])
        self.assertEqual(fetched["related"], [other["id"]])

    def test_no_tags_key_defaults_to_empty_list(self):
        created = self._upsert()
        self.assertEqual(created["tags"], [])
        self.assertEqual(created["related"], [])

    def test_empty_tags_omits_frontmatter_key(self):
        created = self._upsert(tags=[])
        path = self.store._path_for_id(created["id"])
        text = path.read_text(encoding="utf-8")
        frontmatter_text = text.split("---\n")[1]
        self.assertNotIn("tags:", frontmatter_text)

    def test_empty_related_omits_frontmatter_key(self):
        created = self._upsert(related=[])
        path = self.store._path_for_id(created["id"])
        text = path.read_text(encoding="utf-8")
        frontmatter_text = text.split("---\n")[1]
        self.assertNotIn("related:", frontmatter_text)

    def test_non_empty_tags_appear_in_frontmatter(self):
        created = self._upsert(tags=["docker"])
        path = self.store._path_for_id(created["id"])
        text = path.read_text(encoding="utf-8")
        frontmatter_text = text.split("---\n")[1]
        self.assertIn("tags:", frontmatter_text)

    def test_tags_appear_immediately_after_updated_key(self):
        created = self._upsert(tags=["docker"], related=None)
        path = self.store._path_for_id(created["id"])
        text = path.read_text(encoding="utf-8")
        frontmatter_lines = text.split("---\n")[1].splitlines()
        keys = [line.split(":")[0] for line in frontmatter_lines if ":" in line]
        updated_index = keys.index("updated")
        self.assertEqual(keys[updated_index + 1], "tags")

    def test_existing_file_without_tags_or_related_still_reads(self):
        path = self.vault_dir / "memory" / "global" / "legacy.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ntype: profile\ncreated: '2026-01-01T00:00:00+09:00'\n"
            "updated: '2026-01-01T00:00:00+09:00'\n---\n\n# Legacy\n\n本文\n",
            encoding="utf-8",
        )
        fetched = self.store.read("global/legacy")
        self.assertEqual(fetched["tags"], [])
        self.assertEqual(fetched["related"], [])

    def test_reading_a_legacy_file_without_tags_or_related_does_not_modify_it(self):
        path = self.vault_dir / "memory" / "global" / "legacy.md"
        path.parent.mkdir(parents=True)
        original_text = (
            "---\ntype: profile\ncreated: '2026-01-01T00:00:00+09:00'\n"
            "updated: '2026-01-01T00:00:00+09:00'\n---\n\n# Legacy\n\n本文\n"
        )
        path.write_text(original_text, encoding="utf-8")

        self.store.read("global/legacy")
        self.store.iter_all()
        self.store.search()

        self.assertEqual(path.read_text(encoding="utf-8"), original_text)


class TestMarkdownMemoryStoreTagsThreeStateSemantics(MarkdownMemoryStoreTestBase):
    """upsert_from_observation()'s tags/related follow None=keep / list=replace / []=clear."""

    def test_none_keeps_existing_tags(self):
        self._upsert(tags=["docker"])
        second = self._upsert(summary="好みのエディタ: VSCode", tags=None)
        self.assertEqual(second["tags"], ["docker"])

    def test_list_replaces_existing_tags(self):
        self._upsert(tags=["docker"])
        second = self._upsert(summary="好みのエディタ: VSCode", tags=["gpu"])
        self.assertEqual(second["tags"], ["gpu"])

    def test_empty_list_clears_existing_tags(self):
        self._upsert(tags=["docker"])
        second = self._upsert(summary="好みのエディタ: VSCode", tags=[])
        self.assertEqual(second["tags"], [])

    def test_none_keeps_existing_related(self):
        other = self._upsert(key="other_key", summary="other")
        self._upsert(related=[other["id"]])
        second = self._upsert(summary="好みのエディタ: VSCode", related=None)
        self.assertEqual(second["related"], [other["id"]])

    def test_list_replaces_existing_related(self):
        other_a = self._upsert(key="other_a", summary="a")
        other_b = self._upsert(key="other_b", summary="b")
        self._upsert(related=[other_a["id"]])
        second = self._upsert(summary="好みのエディタ: VSCode", related=[other_b["id"]])
        self.assertEqual(second["related"], [other_b["id"]])

    def test_empty_list_clears_existing_related(self):
        other = self._upsert(key="other_key", summary="other")
        self._upsert(related=[other["id"]])
        second = self._upsert(summary="好みのエディタ: VSCode", related=[])
        self.assertEqual(second["related"], [])

    def test_tags_only_change_does_not_bump_updated(self):
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T08:30:00+09:00"):
            self._upsert()
        with patch("markdown_store.current_timestamp", return_value="2026-01-01T09:30:00+09:00"):
            second = self._upsert(tags=["docker"])
        self.assertEqual(second["updated"], "2026-01-01T08:30:00+09:00")

    def test_tags_only_change_does_not_append_history(self):
        self._upsert()
        second = self._upsert(tags=["docker"])
        self.assertEqual(second["history"], [])

    def test_self_reference_is_silently_removed(self):
        created = self._upsert()
        second = self._upsert(summary=created["summary"], related=[created["id"]])
        self.assertEqual(second["related"], [])

    def test_duplicate_related_ids_are_removed(self):
        other = self._upsert(key="other_key", summary="other")
        second = self._upsert(summary="好みのエディタ: VSCode", related=[other["id"], other["id"]])
        self.assertEqual(second["related"], [other["id"]])

    def test_tags_are_sorted_deterministically(self):
        second = self._upsert(tags=["gpu", "docker"])
        self.assertEqual(second["tags"], ["docker", "gpu"])


class TestMarkdownMemoryStoreDanglingRelatedWarning(MarkdownMemoryStoreTestBase):
    """upsert_from_observation()/update_metadata() accept a nonexistent related
    id but warn on stderr (see plan.md design decision 2: existence
    verification is a warning, not a write-time error)."""

    def test_new_record_with_nonexistent_related_id_warns_and_is_accepted(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            created = self._upsert(related=["global/does-not-exist"])

        self.assertEqual(created["related"], ["global/does-not-exist"])
        self.assertIn(created["id"], buffer.getvalue())
        self.assertIn("related", buffer.getvalue())
        self.assertIn("global/does-not-exist", buffer.getvalue())

    def test_existing_record_with_nonexistent_related_id_warns_and_is_accepted(self):
        self._upsert()
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            updated = self._upsert(
                summary="好みのエディタ: VSCode", related=["global/does-not-exist"]
            )

        self.assertEqual(updated["related"], ["global/does-not-exist"])
        self.assertIn("global/does-not-exist", buffer.getvalue())

    def test_valid_related_id_does_not_warn(self):
        other = self._upsert(key="other_key", summary="other")
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            self._upsert(related=[other["id"]])

        self.assertEqual(buffer.getvalue(), "")

    def test_omitted_related_does_not_warn(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            self._upsert()

        self.assertEqual(buffer.getvalue(), "")

    def test_update_metadata_with_nonexistent_related_id_warns_and_is_accepted(self):
        created = self._upsert()
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            record = self.store.update_metadata(created["id"], related=["global/does-not-exist"])

        self.assertEqual(record["related"], ["global/does-not-exist"])
        self.assertIn(created["id"], buffer.getvalue())
        self.assertIn("global/does-not-exist", buffer.getvalue())

    def test_update_metadata_returns_dangling_related_for_nonexistent_id(self):
        created = self._upsert()
        record = self.store.update_metadata(created["id"], related=["global/does-not-exist"])
        self.assertEqual(record["dangling_related"], ["global/does-not-exist"])

    def test_update_metadata_returns_empty_dangling_related_for_valid_id(self):
        other = self._upsert(key="other_key", summary="other")
        created = self._upsert()
        record = self.store.update_metadata(created["id"], related=[other["id"]])
        self.assertEqual(record["dangling_related"], [])

    def test_update_metadata_returns_empty_dangling_related_when_related_not_touched(self):
        created = self._upsert()
        record = self.store.update_metadata(created["id"], tags=["docker"])
        self.assertEqual(record["dangling_related"], [])


class TestMarkdownMemoryStoreLenientRead(MarkdownMemoryStoreTestBase):
    """_read_path() tolerates malformed tags/related, warning instead of failing."""

    def _write_raw(self, memory_id: str, frontmatter_extra: str, title: str = "Title") -> Path:
        path = self.vault_dir / "memory" / f"{memory_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: profile\ncreated: '2026-01-01T00:00:00+09:00'\n"
            f"updated: '2026-01-01T00:00:00+09:00'\n{frontmatter_extra}---\n\n"
            f"# {title}\n\n本文\n",
            encoding="utf-8",
        )
        return path

    def test_missing_tags_key_defaults_to_empty_list(self):
        self._write_raw("global/a", "")
        fetched = self.store.read("global/a")
        self.assertEqual(fetched["tags"], [])

    def test_null_tags_defaults_to_empty_list(self):
        self._write_raw("global/a", "tags: null\n")
        fetched = self.store.read("global/a")
        self.assertEqual(fetched["tags"], [])

    def test_comma_separated_string_tags_are_split(self):
        self._write_raw("global/a", "tags: docker, gpu\n")
        fetched = self.store.read("global/a")
        self.assertEqual(fetched["tags"], ["docker", "gpu"])

    def test_non_container_tags_value_is_dropped_with_warning(self):
        buffer = io.StringIO()
        self._write_raw("global/a", "tags: 123\n")
        with redirect_stderr(buffer):
            fetched = self.store.read("global/a")
        self.assertEqual(fetched["tags"], [])
        self.assertIn("global/a", buffer.getvalue())

    def test_dict_tags_value_is_dropped_with_warning(self):
        buffer = io.StringIO()
        self._write_raw("global/a", "tags:\n  key: value\n")
        with redirect_stderr(buffer):
            fetched = self.store.read("global/a")
        self.assertEqual(fetched["tags"], [])
        self.assertIn("global/a", buffer.getvalue())

    def test_invalid_element_in_tags_list_is_dropped_with_warning(self):
        buffer = io.StringIO()
        self._write_raw("global/a", "tags:\n  - docker\n  - '!!!'\n  - 123\n  - ''\n")
        with redirect_stderr(buffer):
            fetched = self.store.read("global/a")
        self.assertEqual(fetched["tags"], ["docker"])
        self.assertIn("global/a", buffer.getvalue())

    def test_missing_related_key_defaults_to_empty_list(self):
        self._write_raw("global/a", "")
        fetched = self.store.read("global/a")
        self.assertEqual(fetched["related"], [])

    def test_null_related_defaults_to_empty_list(self):
        self._write_raw("global/a", "related: null\n")
        fetched = self.store.read("global/a")
        self.assertEqual(fetched["related"], [])

    def test_string_related_value_is_dropped_with_warning(self):
        buffer = io.StringIO()
        self._write_raw("global/a", "related: global/b\n")
        with redirect_stderr(buffer):
            fetched = self.store.read("global/a")
        self.assertEqual(fetched["related"], [])
        self.assertIn("global/a", buffer.getvalue())

    def test_malformed_related_element_is_dropped_with_warning_others_kept(self):
        buffer = io.StringIO()
        self._write_raw("global/a", "related:\n  - global/b\n  - bare-slug\n")
        with redirect_stderr(buffer):
            fetched = self.store.read("global/a")
        self.assertEqual(fetched["related"], ["global/b"])
        self.assertIn("global/a", buffer.getvalue())

    def test_related_pointing_to_a_nonexistent_but_well_formed_id_is_kept(self):
        fetched_path = self._write_raw("global/a", "related:\n  - global/does-not-exist\n")
        fetched = self.store.read("global/a")
        self.assertEqual(fetched["related"], ["global/does-not-exist"])
        self.assertTrue(fetched_path.exists())

    def test_reading_malformed_tags_does_not_modify_the_file(self):
        path = self._write_raw("global/a", "tags:\n  - docker\n  - '!!!'\n")
        original_text = path.read_text(encoding="utf-8")
        self.store.read("global/a")
        self.assertEqual(path.read_text(encoding="utf-8"), original_text)

    def test_other_records_remain_searchable_when_one_has_malformed_tags(self):
        self._write_raw("global/a", "tags: 123\n", title="Broken")
        self._upsert(key="preferred_editor", summary="好みのエディタ: Neovim")
        results = self.store.search()
        titles = [r["title"] for r in results]
        self.assertIn("Broken", titles)
        self.assertIn("Preferred Editor", titles)

    def test_malformed_tags_do_not_break_index_generation(self):
        self._write_raw("global/a", "tags: 123\n")
        # Any subsequent write triggers _write_index(); it must not raise.
        self._upsert(key="preferred_editor", summary="好みのエディタ: Neovim")
        index_text = (self.vault_dir / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("global/a.md", index_text)

    def test_malformed_tags_do_not_break_list_tags(self):
        self._write_raw("global/a", "tags: 123\n")
        self._upsert(key="preferred_editor", summary="好みのエディタ: Neovim", tags=["docker"])
        tags = self.store.list_tags()
        self.assertEqual(tags, [{"tag": "docker", "count": 1}])


class TestMarkdownMemoryStoreSearchTags(MarkdownMemoryStoreTestBase):
    """search(tags=...) applies an AND filter across the provided tags."""

    def test_returns_only_memories_with_all_given_tags(self):
        self._upsert(key="a", summary="a", tags=["docker", "gpu"])
        self._upsert(key="b", summary="b", tags=["docker"])
        self._upsert(key="c", summary="c", tags=["gpu"])

        results = self.store.search(tags=["docker", "gpu"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "A")

    def test_no_tags_filter_returns_everything(self):
        self._upsert(key="a", summary="a", tags=["docker"])
        results = self.store.search()
        self.assertEqual(len(results), 1)

    def test_tag_with_no_matches_returns_empty(self):
        self._upsert(key="a", summary="a", tags=["docker"])
        results = self.store.search(tags=["nonexistent"])
        self.assertEqual(results, [])


class TestMarkdownMemoryStoreRelated(MarkdownMemoryStoreTestBase):
    """related() ranks by explicit links and shared tags."""

    def test_outgoing_link_is_reported_on_the_target(self):
        b = self._upsert(key="b_key", summary="b")
        a = self._upsert(key="a_key", summary="a", related=[b["id"]])

        result = self.store.related(b["id"])
        hit_ids = [h["id"] for h in result["hits"]]
        self.assertIn(a["id"], hit_ids)
        hit = next(h for h in result["hits"] if h["id"] == a["id"])
        self.assertEqual(hit["link"], "incoming")

    def test_source_side_reports_outgoing_link(self):
        b = self._upsert(key="b_key", summary="b")
        a = self._upsert(key="a_key", summary="a", related=[b["id"]])

        result = self.store.related(a["id"])
        hit = next(h for h in result["hits"] if h["id"] == b["id"])
        self.assertEqual(hit["link"], "outgoing")

    def test_mutual_link_scores_higher_than_shared_tag_only(self):
        b = self._upsert(key="b_key", summary="b", tags=["docker"])
        a = self._upsert(key="a_key", summary="a", related=[b["id"]], tags=["docker"])
        self.store.update_metadata(b["id"], related=[a["id"]])
        c = self._upsert(key="c_key", summary="c", tags=["docker"])

        result = self.store.related(a["id"])
        by_id = {h["id"]: h for h in result["hits"]}
        self.assertEqual(by_id[b["id"]]["link"], "mutual")
        self.assertGreater(by_id[b["id"]]["score"], by_id[c["id"]]["score"])

    def test_shared_tags_alone_produce_a_hit_with_matched_tags(self):
        self._upsert(key="a_key", summary="a", tags=["docker", "gpu"])
        b = self._upsert(key="b_key", summary="b", tags=["docker"])

        result = self.store.related(b["id"])
        hit = next(h for h in result["hits"])
        self.assertEqual(hit["matched_tags"], ["docker"])
        self.assertEqual(hit["link"], "none")

    def test_no_shared_tags_or_links_produces_no_hit(self):
        self._upsert(key="a_key", summary="a", tags=["docker"])
        b = self._upsert(key="b_key", summary="b", tags=["gpu"])

        result = self.store.related(b["id"])
        self.assertEqual(result["hits"], [])

    def test_dangling_reference_is_separated_from_hits(self):
        target = self._upsert(key="target_key", summary="target")
        source = self._upsert(key="source_key", summary="source", related=[target["id"]])
        self.store.forget(target["id"])

        result = self.store.related(source["id"])
        hit_ids = [h["id"] for h in result["hits"]]
        self.assertNotIn(target["id"], hit_ids)
        self.assertIn(target["id"], result["dangling"])

    def test_limit_caps_the_number_of_hits(self):
        source = self._upsert(key="source_key", summary="source", tags=["docker"])
        for i in range(5):
            self._upsert(key=f"other_{i}", summary=f"other {i}", tags=["docker"])

        result = self.store.related(source["id"], limit=2)
        self.assertEqual(len(result["hits"]), 2)

    def test_raises_for_nonexistent_memory_id(self):
        with self.assertRaises(StorePathError):
            self.store.related("global/does-not-exist")

    def test_tiebreak_uses_utc_instant_not_string_order(self):
        # 2026-09-06T10:00:00+09:00 is 01:00 UTC; 2026-09-06T02:00:00+00:00 is
        # 02:00 UTC and therefore later, even though it sorts earlier as a
        # bare string (see verification.md issue 2).
        source = self._upsert(key="source_key", summary="source", tags=["docker"])
        older = self._upsert(key="older_key", summary="older", tags=["docker"])
        newer = self._upsert(key="newer_key", summary="newer", tags=["docker"])
        older["updated"] = "2026-09-06T10:00:00+09:00"
        self.store.write(older)
        newer["updated"] = "2026-09-06T02:00:00+00:00"
        self.store.write(newer)

        result = self.store.related(source["id"])

        order = [hit["id"] for hit in result["hits"]]
        self.assertLess(order.index(newer["id"]), order.index(older["id"]))

    def test_tiebreak_treats_same_instant_in_different_offsets_as_tied(self):
        source = self._upsert(key="source_key", summary="source", tags=["docker"])
        a = self._upsert(key="a_key", summary="a", tags=["docker"])
        b = self._upsert(key="b_key", summary="b", tags=["docker"])
        a["updated"] = "2026-09-06T10:00:00+09:00"  # 01:00 UTC
        self.store.write(a)
        b["updated"] = "2026-09-06T01:00:00+00:00"  # same instant, 01:00 UTC
        self.store.write(b)

        result = self.store.related(source["id"])

        # Tied score and tied instant -> tiebreak falls through to id
        # ascending, regardless of which of a/b was written to disk last.
        order = [hit["id"] for hit in result["hits"]]
        self.assertEqual(order, sorted([a["id"], b["id"]]))

    def test_tiebreak_handles_legacy_date_only_updated_value(self):
        source = self._upsert(key="source_key", summary="source", tags=["docker"])
        legacy = self._upsert(key="legacy_key", summary="legacy", tags=["docker"])
        recent = self._upsert(key="recent_key", summary="recent", tags=["docker"])
        legacy["updated"] = "2020-01-01"
        self.store.write(legacy)
        recent["updated"] = "2026-09-06T00:00:00+00:00"
        self.store.write(recent)

        result = self.store.related(source["id"])

        order = [hit["id"] for hit in result["hits"]]
        self.assertLess(order.index(recent["id"]), order.index(legacy["id"]))

    def test_tiebreak_handles_missing_updated_value_without_raising(self):
        # A single hit would never actually invoke the sort comparison, so
        # this needs a second, normally-timestamped hit for the None-vs-str
        # comparison to actually occur (and previously raise TypeError).
        source = self._upsert(key="source_key", summary="source", tags=["docker"])
        broken = self._upsert(key="broken_key", summary="broken", tags=["docker"])
        normal = self._upsert(key="normal_key", summary="normal", tags=["docker"])
        broken["updated"] = None
        self.store.write(broken)

        result = self.store.related(source["id"])

        hit_ids = [hit["id"] for hit in result["hits"]]
        self.assertEqual(set(hit_ids), {broken["id"], normal["id"]})

    def test_rejects_zero_limit(self):
        created = self._upsert()
        with self.assertRaises(ValueError):
            self.store.related(created["id"], limit=0)

    def test_rejects_negative_limit(self):
        created = self._upsert()
        with self.assertRaises(ValueError):
            self.store.related(created["id"], limit=-1)


class TestMarkdownMemoryStoreListTags(MarkdownMemoryStoreTestBase):
    """list_tags() aggregates tag usage counts across all active memories."""

    def test_counts_and_sorts_by_count_descending(self):
        self._upsert(key="a", summary="a", tags=["docker", "gpu"])
        self._upsert(key="b", summary="b", tags=["docker"])

        tags = self.store.list_tags()

        self.assertEqual(tags, [{"tag": "docker", "count": 2}, {"tag": "gpu", "count": 1}])

    def test_ties_are_sorted_alphabetically(self):
        self._upsert(key="a", summary="a", tags=["zeta", "alpha"])

        tags = self.store.list_tags()

        self.assertEqual(tags, [{"tag": "alpha", "count": 1}, {"tag": "zeta", "count": 1}])

    def test_empty_store_returns_empty_list(self):
        self.assertEqual(self.store.list_tags(), [])

    def test_forgotten_memory_tags_are_excluded(self):
        created = self._upsert(tags=["docker"])
        self.store.forget(created["id"])
        self.assertEqual(self.store.list_tags(), [])


class TestMarkdownMemoryStoreIndexTags(MarkdownMemoryStoreTestBase):
    """_index.md shows an inline-code tag suffix for tagged memories."""

    def test_tagged_memory_shows_inline_tags_suffix(self):
        self._upsert(tags=["docker", "gpu"])
        index_text = (self.vault_dir / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("`tags: docker, gpu`", index_text)

    def test_untagged_memory_has_no_tags_suffix(self):
        self._upsert()
        index_text = (self.vault_dir / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("`tags:", index_text)

    def test_related_is_not_shown_in_index(self):
        other = self._upsert(key="other_key", summary="other")
        self._upsert(related=[other["id"]])
        index_text = (self.vault_dir / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("related", index_text)


class TestMarkdownMemoryStoreUpdateMetadata(MarkdownMemoryStoreTestBase):
    """update_metadata() replaces only tags/related, preserving the rest byte-for-byte."""

    def _write_hand_edited(self, memory_id: str) -> Path:
        path = self.vault_dir / "memory" / f"{memory_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: reference\ncreated: '2026-01-01T09:00:00+09:00'\n"
            "updated: '2026-01-05T09:00:00+09:00'\ncustom_field: kept\n---\n\n"
            "# Hand Edited Title\n\n本文の要約\n\n## 変更履歴\n\n"
            "- 2026-01-02: 旧要約 → 本文の要約 に変更\n",
            encoding="utf-8",
        )
        return path

    def test_replaces_tags_and_preserves_body_byte_for_byte(self):
        path = self._write_hand_edited("global/hand-edited")
        original_bytes = path.read_bytes()

        self.store.update_metadata("global/hand-edited", tags=["docker"])

        new_bytes = path.read_bytes()
        original_body = original_bytes.split(b"---\n", 2)[-1]
        new_body = new_bytes.split(b"---\n", 2)[-1]
        self.assertEqual(new_body, original_body)

    def test_preserves_whitespace_irregular_body_byte_for_byte(self):
        # A body with a blank line right after the frontmatter delimiter, an
        # embedded blank line, and two trailing blank lines at EOF -- none of
        # this is the store's own normalized shape (see write()/_render_
        # markdown()), so it exercises whitespace _parse_markdown() would
        # otherwise normalize away (lstrip/rstrip on "\n").
        path = self.vault_dir / "memory" / "global" / "whitespace-edited.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = (
            "---\ntype: reference\ncreated: '2026-01-01T09:00:00+09:00'\n"
            "updated: '2026-01-05T09:00:00+09:00'\n---\n\n"
            "# Whitespace Edited\n\n本文の一段落目\n\n本文の二段落目\n\n\n"
        ).encode("utf-8")
        path.write_bytes(original_bytes)

        self.store.update_metadata("global/whitespace-edited", tags=["docker"])

        new_bytes = path.read_bytes()
        new_body = new_bytes.split(b"---\n", 2)[-1]
        original_body = original_bytes.split(b"---\n", 2)[-1]
        self.assertEqual(new_body, original_body)

    def test_preserves_crlf_body_byte_for_byte(self):
        # The body was hand-edited on Windows (CRLF line endings); only the
        # store-managed frontmatter uses LF. Path.read_text()'s universal
        # newlines would silently translate "\r\n" -> "\n" on read, and the
        # translation is invisible unless bytes are compared directly.
        path = self.vault_dir / "memory" / "global" / "crlf-edited.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = (
            b"---\ntype: reference\ncreated: '2026-01-01T09:00:00+09:00'\n"
            b"updated: '2026-01-05T09:00:00+09:00'\n---\n"
            b"\r\n# CRLF Title\r\n\r\nBody line one\r\nBody line two\r\n"
        )
        path.write_bytes(original_bytes)

        self.store.update_metadata("global/crlf-edited", tags=["docker"])

        new_bytes = path.read_bytes()
        original_body = original_bytes.split(b"---\n", 2)[-1]
        new_body = new_bytes.split(b"---\n", 2)[-1]
        self.assertEqual(new_body, original_body)

    def test_preserves_mixed_newlines_in_body_byte_for_byte(self):
        path = self.vault_dir / "memory" / "global" / "mixed-edited.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = (
            b"---\ntype: reference\ncreated: '2026-01-01T09:00:00+09:00'\n"
            b"updated: '2026-01-05T09:00:00+09:00'\n---\n"
            b"\n# Mixed Title\r\n\nBody line one\r\nBody line two\n"
        )
        path.write_bytes(original_bytes)

        self.store.update_metadata("global/mixed-edited", tags=["docker"])

        new_bytes = path.read_bytes()
        original_body = original_bytes.split(b"---\n", 2)[-1]
        new_body = new_bytes.split(b"---\n", 2)[-1]
        self.assertEqual(new_body, original_body)

    def test_preserves_type_created_updated_and_unknown_frontmatter_key(self):
        self._write_hand_edited("global/hand-edited")

        record = self.store.update_metadata("global/hand-edited", tags=["docker"])

        self.assertEqual(record["type"], "reference")
        self.assertEqual(record["created"], "2026-01-01T09:00:00+09:00")
        self.assertEqual(record["updated"], "2026-01-05T09:00:00+09:00")
        path = self.store._path_for_id("global/hand-edited")
        self.assertIn("custom_field: kept", path.read_text(encoding="utf-8"))

    def test_none_keeps_field_untouched(self):
        self._write_hand_edited("global/hand-edited")
        self.store.update_metadata("global/hand-edited", tags=["docker"])

        record = self.store.update_metadata("global/hand-edited", related=["global/other"])

        self.assertEqual(record["tags"], ["docker"])

    def test_list_replaces_field(self):
        self._write_hand_edited("global/hand-edited")
        self.store.update_metadata("global/hand-edited", tags=["docker"])

        record = self.store.update_metadata("global/hand-edited", tags=["gpu", "linux"])

        self.assertEqual(record["tags"], ["gpu", "linux"])

    def test_empty_list_clears_field(self):
        self._write_hand_edited("global/hand-edited")
        self.store.update_metadata("global/hand-edited", tags=["docker"])

        record = self.store.update_metadata("global/hand-edited", tags=[])

        self.assertEqual(record["tags"], [])
        path = self.store._path_for_id("global/hand-edited")
        self.assertNotIn("tags:", path.read_text(encoding="utf-8").split("---\n")[1])

    def test_both_none_raises(self):
        self._write_hand_edited("global/hand-edited")
        with self.assertRaises(ValueError):
            self.store.update_metadata("global/hand-edited")

    def test_nonexistent_id_raises(self):
        with self.assertRaises(ValueError):
            self.store.update_metadata("global/does-not-exist", tags=["docker"])

    def test_malformed_memory_id_raises(self):
        with self.assertRaises(StorePathError):
            self.store.update_metadata("bare-slug", tags=["docker"])

    def _assert_rejected_without_side_effects(self, memory_id: str, original_bytes: bytes) -> None:
        index_path = self.vault_dir / "memory" / INDEX_FILENAME
        index_before = index_path.read_text(encoding="utf-8") if index_path.exists() else None

        with self.assertRaises(ValueError):
            self.store.update_metadata(memory_id, tags=["docker"])

        path = self.store._path_for_id(memory_id)
        self.assertEqual(path.read_bytes(), original_bytes)
        index_after = index_path.read_text(encoding="utf-8") if index_path.exists() else None
        self.assertEqual(index_after, index_before)

    def test_bom_prefixed_file_is_rejected_without_side_effects(self):
        path = self.vault_dir / "memory" / "global" / "bom-edited.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = (
            "﻿---\ntype: reference\ncreated: '2026-01-01T09:00:00+09:00'\n"
            "updated: '2026-01-05T09:00:00+09:00'\n---\n\n# BOM Title\n\n本文\n"
        ).encode("utf-8")
        path.write_bytes(original_bytes)

        self._assert_rejected_without_side_effects("global/bom-edited", original_bytes)

    def test_plain_markdown_without_frontmatter_is_rejected_without_side_effects(self):
        path = self.vault_dir / "memory" / "global" / "no-frontmatter.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = "# No Frontmatter\n\nこれはただの本文です。\n".encode()
        path.write_bytes(original_bytes)

        self._assert_rejected_without_side_effects("global/no-frontmatter", original_bytes)

    def test_unterminated_frontmatter_is_rejected_without_side_effects(self):
        path = self.vault_dir / "memory" / "global" / "unterminated.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = (
            "---\ntype: reference\ncreated: '2026-01-01T09:00:00+09:00'\n"
            "updated: '2026-01-05T09:00:00+09:00'\n\n# Unterminated\n\n本文\n"
        ).encode("utf-8")
        path.write_bytes(original_bytes)

        self._assert_rejected_without_side_effects("global/unterminated", original_bytes)

    def test_repeated_identical_update_does_not_increase_memory_count(self):
        self._write_hand_edited("global/hand-edited")
        self.store.update_metadata("global/hand-edited", tags=["docker"])
        self.store.update_metadata("global/hand-edited", tags=["docker"])

        self.assertEqual(len(_non_index_files(self.vault_dir / "memory")), 1)

    def test_repeated_identical_update_does_not_rewrite_the_file(self):
        self._write_hand_edited("global/hand-edited")
        self.store.update_metadata("global/hand-edited", tags=["docker"])
        path = self.store._path_for_id("global/hand-edited")
        first_text = path.read_text(encoding="utf-8")

        self.store.update_metadata("global/hand-edited", tags=["docker"])

        self.assertEqual(path.read_text(encoding="utf-8"), first_text)

    def test_related_only_update_does_not_touch_unrelated_malformed_tags(self):
        path = self.vault_dir / "memory" / "global" / "hand-edited.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: reference\ncreated: '2026-01-01T09:00:00+09:00'\n"
            "updated: '2026-01-05T09:00:00+09:00'\ntags: 123\n---\n\n"
            "# Hand Edited Title\n\n本文の要約\n",
            encoding="utf-8",
        )
        other = self._upsert(key="other_key", summary="other")

        self.store.update_metadata("global/hand-edited", related=[other["id"]])

        raw_text = self.store._path_for_id("global/hand-edited").read_text(encoding="utf-8")
        self.assertIn("tags: 123", raw_text)

    def test_self_reference_is_removed_from_related(self):
        self._write_hand_edited("global/hand-edited")

        record = self.store.update_metadata("global/hand-edited", related=["global/hand-edited"])

        self.assertEqual(record["related"], [])

    def test_returned_record_reflects_updated_tags(self):
        self._write_hand_edited("global/hand-edited")

        record = self.store.update_metadata("global/hand-edited", tags=["docker", "gpu"])

        self.assertEqual(record["tags"], ["docker", "gpu"])
        fetched = self.store.read("global/hand-edited")
        self.assertEqual(fetched["tags"], ["docker", "gpu"])


if __name__ == "__main__":
    unittest.main()
