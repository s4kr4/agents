"""Storage configuration must fail closed before creating any storage."""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch


class TestMemoryConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config")}
        self.patch = patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("memory_config"))
        return importlib.import_module("memory_config")

    def config(self, text):
        path = self.root / "config" / "llm-memory" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_precedence_and_expansion_without_creating_directories(self):
        cfg = self.module()
        self.config('vault = "~/vault"\nlocal_dir = "$HOME/local"\nqueue_dir = "${HOME}/queue"\n')
        paths = cfg.resolve_paths()
        self.assertEqual(paths.vault, self.root / "vault")
        self.assertEqual(paths.local_dir, self.root / "local")
        self.assertEqual(paths.queue_dir, self.root / "queue")
        self.assertFalse(paths.used_fallback)
        self.assertFalse(paths.vault.exists())
        os.environ["LLM_MEMORY_VAULT"] = str(self.root / "env-vault")
        self.assertEqual(cfg.resolve_paths().vault, self.root / "env-vault")
        self.assertEqual(
            cfg.resolve_paths(vault=self.root / "explicit").vault, self.root / "explicit"
        )

    def test_invalid_configuration_never_falls_back_even_with_override(self):
        cfg = self.module()
        os.environ["LLM_MEMORY_VAULT"] = str(self.root / "override")
        for text in (
            "vault = [",
            "vault = 42",
            'unknown = "/tmp"',
            'vault = ""',
            'vault = "relative"',
            'vault = "$UNRESOLVED/value"',
        ):
            with self.subTest(text=text):
                self.config(text)
                with self.assertRaises(cfg.MemoryConfigError):
                    cfg.resolve_paths()
                self.assertFalse((self.root / "override").exists())

    def test_explicit_missing_configuration_and_directory_are_errors(self):
        cfg = self.module()
        for value in (str(self.root / "missing.toml"), str(self.root), ""):
            with self.subTest(value=value):
                os.environ["LLM_MEMORY_CONFIG"] = value
                with self.assertRaises(cfg.MemoryConfigError):
                    cfg.resolve_paths()

    def test_no_configuration_preserves_fallback_but_mcp_requires_vault(self):
        cfg = self.module()
        self.assertTrue(cfg.resolve_paths().used_fallback)
        with self.assertRaises(cfg.MemoryConfigError):
            cfg.resolve_paths(require_vault=True)

    def test_missing_tomllib_is_allowed_only_when_configuration_absent(self):
        cfg = self.module()
        with patch.object(cfg, "tomllib", None):
            self.assertTrue(cfg.resolve_paths().used_fallback)
            self.config('vault = "~/vault"')
            with self.assertRaisesRegex(cfg.MemoryConfigError, "Python|uv"):
                cfg.resolve_paths()

    def test_unreadable_configuration_does_not_fall_back(self):
        cfg = self.module()
        self.config('vault = "~/vault"')
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(cfg.MemoryConfigError):
                cfg.resolve_paths()

    def test_invalid_override_is_not_replaced_with_valid_config_value(self):
        cfg = self.module()
        self.config('vault = "~/vault"')
        for value in ("", "relative", "$UNRESOLVED/vault"):
            with self.subTest(value=value):
                os.environ["LLM_MEMORY_LOCAL_DIR"] = value
                with self.assertRaises(cfg.MemoryConfigError):
                    cfg.resolve_paths()

    def test_existing_file_cannot_be_storage_directory(self):
        cfg = self.module()
        target = self.root / "file"
        target.write_text("untouched")
        os.environ["LLM_MEMORY_VAULT"] = str(target)
        with self.assertRaises(cfg.MemoryConfigError):
            cfg.resolve_paths()
        self.assertEqual(target.read_text(), "untouched")

    def test_os_config_locations_are_pure_path_calculations(self):
        cfg = self.module()
        self.assertEqual(
            cfg.default_config_path("nt", {"APPDATA": "C:/Users/a/Roaming"}, "C:/Users/a"),
            PureWindowsPath("C:/Users/a/Roaming/llm-memory/config.toml"),
        )
        self.assertEqual(
            cfg.default_config_path("nt", {}, "C:/Users/a"),
            PureWindowsPath("C:/Users/a/AppData/Roaming/llm-memory/config.toml"),
        )
        self.assertEqual(
            cfg.default_config_path("posix", {}, "/Users/a"),
            Path("/Users/a/.config/llm-memory/config.toml"),
        )


if __name__ == "__main__":
    unittest.main()
