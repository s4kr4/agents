import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "config", Path(__file__).with_name("memory_mcp_config.py")
)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            Path(SPEC.origin).exists(), "configuration generator must exist"
        )
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.entry = self.module.server_entry("/opt/日本語 path/uv", "/opt/agents path")

    def test_generation_preserves_argument_boundaries(self):
        self.assertEqual(self.entry["command"], "/opt/日本語 path/uv")
        self.assertEqual(
            self.entry["args"][-1], "/opt/agents path/memory/mcp_server.py"
        )

    def test_apply_preserves_unknown_settings_and_is_noop(self):
        old = {
            "other": 1,
            "mcpServers": {
                "other": {"command": "other"},
                "shared-memory": {"env": {"LANG": "ja_JP"}, "timeout": 90},
            },
        }
        self.path.write_text(json.dumps(old))
        self.module.apply_config(
            self.path, "claude-desktop", self.entry, non_secret=True
        )
        data = json.loads(self.path.read_text())
        self.assertEqual(data["mcpServers"]["shared-memory"]["env"], {"LANG": "ja_JP"})
        self.assertEqual(data["mcpServers"]["shared-memory"]["timeout"], 90)
        self.assertEqual(data["mcpServers"]["other"], old["mcpServers"]["other"])
        before = self.path.stat().st_mtime_ns
        self.assertEqual(
            self.module.apply_config(
                self.path, "claude-desktop", self.entry, non_secret=True
            ),
            "unchanged",
        )
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_existing_configuration_requires_nonsecret_attestation_before_read(self):
        self.path.write_text("{}")
        with (
            patch.object(
                Path, "read_bytes", side_effect=AssertionError("must not read")
            ),
            self.assertRaisesRegex(ValueError, "non-secret"),
        ):
            self.module.apply_config(self.path, "claude-desktop", self.entry)

    def test_malformed_configuration_is_not_overwritten(self):
        self.path.write_text("{")
        with self.assertRaises(ValueError):
            self.module.apply_config(
                self.path, "claude-desktop", self.entry, non_secret=True
            )
        self.assertEqual(self.path.read_text(), "{")

    def test_concurrent_change_is_preserved(self):
        self.path.write_text("{}")

        def changed(*args):
            self.path.write_text('{"external": true}')

        with (
            patch.object(self.module, "_before_replace", side_effect=changed),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            self.module.apply_config(
                self.path, "claude-desktop", self.entry, non_secret=True
            )
        self.assertEqual(json.loads(self.path.read_text()), {"external": True})

    def test_post_validation_failure_rolls_back(self):
        self.path.write_text("{}")
        with (
            patch.object(
                self.module, "_verify", side_effect=ValueError("verification failed")
            ),
            self.assertRaisesRegex(ValueError, "verification failed"),
        ):
            self.module.apply_config(
                self.path, "claude-desktop", self.entry, non_secret=True
            )
        self.assertEqual(self.path.read_text(), "{}")

    def test_toml_comments_permissions_and_unknown_tables_preserved(self):
        self.path = self.path.with_suffix(".toml")
        self.path.write_text(
            '# keep\n[other]\nvalue = 1\n[mcp_servers.shared-memory]\ncommand="old"\nstartup_timeout_sec=90\n[mcp_servers.shared-memory.tools.forget]\napproval_policy="deny"\n'
        )
        self.module.apply_config(self.path, "codex", self.entry, non_secret=True)
        content = self.path.read_text()
        self.assertIn("# keep", content)
        self.assertIn('approval_policy="deny"', content)
        self.assertIn("startup_timeout_sec=90", content)

    def test_symlink_requires_explicit_real_target(self):
        self.path.write_text("{}")
        link = self.path.with_name("linked.json")
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "real file"):
            self.module.apply_config(
                link, "claude-desktop", self.entry, non_secret=True
            )
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_text(), "{}")

    def test_failed_verification_does_not_overwrite_external_change(self):
        self.path.write_text("{}")

        def changed(*args):
            self.path.write_text('{"external": true}')
            raise ValueError("verification failed")

        with (
            patch.object(self.module, "_verify", side_effect=changed),
            self.assertRaisesRegex(ValueError, "rollback refused"),
        ):
            self.module.apply_config(
                self.path, "claude-desktop", self.entry, non_secret=True
            )
        self.assertEqual(json.loads(self.path.read_text()), {"external": True})

    def test_missing_uv_reports_skipped(self):
        result = subprocess.run(
            [
                "/bin/bash",
                str(Path(__file__).with_name("deploy-memory-mcp.sh")),
                "--client",
                "codex",
            ],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("skipped: uv", result.stderr)

    def test_deploy_script_does_not_write_to_the_callers_declared_storage(self):
        """The installer's own MCP handshake (see memory-mcp-check.sh) must
        run against an isolated throwaway Vault/local/queue/config, never
        whatever LLM_MEMORY_VAULT/LOCAL_DIR/QUEUE_DIR/XDG_* the caller's
        shell happens to have set -- see R-1: this used to write a real
        "editor" profile memory and a throwaway session into the real Vault
        and the real local dir on every first-time setup run, which
        Syncthing would then propagate the Vault half of that to every
        other machine.

        Every one of the five paths below stands in for what a real user's
        shell/config would already have pointed at (mirroring
        ~/.codex/config.toml's actual shared-memory env block on this
        machine); asserting none of them get created is what would catch a
        regression in memory-mcp-check.sh silently dropping one of its own
        env overrides (e.g. the LLM_MEMORY_LOCAL_DIR line -- see the
        contamination this test's own earlier, narrower version missed).

        PowerShell has no automated equivalent here: deploy-memory-mcp.ps1
        cannot be executed on this (non-Windows) machine, so its isolation
        (APPDATA/LOCALAPPDATA redirection) is reviewed by inspection only.
        """
        for client in ("codex", "claude-code"):
            with self.subTest(client=client):
                isolated_root = Path(self.tmp.name) / f"deploy-isolation-{client}"
                declared = {
                    "LLM_MEMORY_VAULT": isolated_root / "vault-a",
                    "LLM_MEMORY_LOCAL_DIR": isolated_root / "local-a",
                    "LLM_MEMORY_QUEUE_DIR": isolated_root / "queue-a",
                    "XDG_CONFIG_HOME": isolated_root / "xdg-config-a",
                    "XDG_CACHE_HOME": isolated_root / "xdg-cache-a",
                }
                env = dict(os.environ)
                env.update((key, str(path)) for key, path in declared.items())
                env.pop("LLM_MEMORY_CONFIG", None)
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(Path(__file__).with_name("deploy-memory-mcp.sh")),
                        "--client",
                        client,
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                expected_key = "mcp_servers" if client == "codex" else "mcpServers"
                self.assertIn(expected_key, result.stdout)
                for name, path in declared.items():
                    if name == "XDG_CACHE_HOME":
                        # uv itself (not the shared-memory isolation) keeps
                        # its own package cache under XDG_CACHE_HOME, so the
                        # directory legitimately exists after "uv sync"; what
                        # must never appear is store_lock.py's own
                        # "llm-memory" subdirectory (see lock_directory()),
                        # which is the memory-specific artifact this test
                        # actually guards against.
                        self.assertFalse(
                            (path / "llm-memory").exists(),
                            f"{name}={path}/llm-memory should not have been created",
                        )
                        continue
                    self.assertFalse(
                        path.exists(), f"{name}={path} should not have been created"
                    )

    def test_remote_transport_is_rejected_without_changes(self):
        content = '{"mcpServers":{"shared-memory":{"type":"http","url":"https://example.invalid/mcp"}}}'
        self.path.write_text(content)
        with self.assertRaisesRegex(ValueError, "transport"):
            self.module.apply_config(
                self.path, "claude-desktop", self.entry, non_secret=True
            )
        self.assertEqual(self.path.read_text(), content)

    def test_unknown_client_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "client"):
            self.module.apply_config(self.path, "unknown", self.entry)

    def test_existing_file_permissions_are_preserved(self):
        self.path.write_text("{}")
        self.path.chmod(0o640)
        self.module.apply_config(
            self.path, "claude-desktop", self.entry, non_secret=True
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
