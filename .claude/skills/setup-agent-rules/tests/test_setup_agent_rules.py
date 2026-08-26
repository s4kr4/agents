from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "setup_agent_rules.py"


class SetupAgentRulesCliTest(unittest.TestCase):
    def run_cli(self, project: Path, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, "--project", str(project)],
            check=False,
            capture_output=True,
            text=True,
        )

    def initialize_valid_project(self, project: Path) -> None:
        result = self.run_cli(project, "init")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_init_creates_shared_rule_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            result = self.run_cli(project, "init")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((project / "docs/rules/INDEX.md").is_file())
            rules_link = project / ".claude/rules"
            self.assertTrue(rules_link.is_symlink())
            self.assertEqual(rules_link.readlink(), Path("../docs/rules"))
            self.assertIn("docs/rules/INDEX.md", (project / "AGENTS.md").read_text())
            self.assertIn("@AGENTS.md", (project / "CLAUDE.md").read_text())
            validation = self.run_cli(project, "validate")
            self.assertEqual(validation.returncode, 0, validation.stderr)

    def test_init_is_idempotent_and_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            (project / "AGENTS.md").write_text("# Existing agents\n", encoding="utf-8")
            (project / "CLAUDE.md").write_text("# Existing Claude\n", encoding="utf-8")
            first = self.run_cli(project, "init")
            agents_after_first = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude_after_first = (project / "CLAUDE.md").read_text(encoding="utf-8")
            second = self.run_cli(project, "init")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((project / "AGENTS.md").read_text(), agents_after_first)
            self.assertEqual((project / "CLAUDE.md").read_text(), claude_after_first)
            self.assertTrue(agents_after_first.startswith("# Existing agents\n"))
            self.assertTrue(claude_after_first.startswith("# Existing Claude\n"))

    def test_init_stops_before_writing_when_rules_path_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            conflict = project / ".claude/rules"
            conflict.mkdir(parents=True)
            (conflict / "keep.md").write_text("keep", encoding="utf-8")
            result = self.run_cli(project, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("競合", result.stderr)
            self.assertEqual((conflict / "keep.md").read_text(), "keep")
            self.assertFalse((project / "docs").exists())
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())

    def test_init_rejects_unexpected_symlink_without_replacing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            link = project / ".claude/rules"
            link.parent.mkdir(parents=True)
            link.symlink_to("../other-rules")
            result = self.run_cli(project, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(link.readlink(), Path("../other-rules"))
            self.assertFalse((project / "docs").exists())

    def test_init_stops_before_writing_when_index_path_is_not_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            index = project / "docs/rules/INDEX.md"
            index.mkdir(parents=True)
            result = self.run_cli(project, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("競合", result.stderr)
            self.assertTrue(index.is_dir())
            self.assertFalse((project / ".claude").exists())
            self.assertFalse((project / "AGENTS.md").exists())

    def test_init_stops_when_docs_rules_is_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            external_rules = project / "external-rules"
            external_rules.mkdir()
            rules = project / "docs/rules"
            rules.parent.mkdir()
            rules.symlink_to("../external-rules")
            result = self.run_cli(project, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("競合", result.stderr)
            self.assertEqual(list(external_rules.iterdir()), [])

    def test_init_does_not_follow_agents_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            external = project / "external.md"
            external.write_text("keep", encoding="utf-8")
            (project / "AGENTS.md").symlink_to("external.md")
            result = self.run_cli(project, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("競合", result.stderr)
            self.assertEqual(external.read_text(encoding="utf-8"), "keep")
            self.assertFalse((project / "docs").exists())

    def test_init_does_not_follow_docs_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            external_docs = project / "external-docs"
            external_docs.mkdir()
            (project / "docs").symlink_to("external-docs")
            result = self.run_cli(project, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("競合", result.stderr)
            self.assertEqual(list(external_docs.iterdir()), [])
            self.assertFalse((project / ".claude").exists())
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())

    def test_init_does_not_follow_claude_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            external_claude = project / "external-claude"
            external_claude.mkdir()
            (project / ".claude").symlink_to("external-claude")
            result = self.run_cli(project, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("競合", result.stderr)
            self.assertEqual(list(external_claude.iterdir()), [])
            self.assertFalse((project / "docs").exists())
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())

    def test_validate_rejects_symlinks_and_wrong_path_types(self) -> None:
        cases = (
            "docs_symlink",
            "rules_file",
            "index_symlink",
            "agents_symlink",
            "claude_symlink",
            "claude_directory_symlink",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                self.initialize_valid_project(project)
                external = project / "external"
                if case == "docs_symlink":
                    (project / "docs/rules/INDEX.md").unlink()
                    (project / "docs/rules").rmdir()
                    (project / "docs").rmdir()
                    external.mkdir()
                    (project / "docs").symlink_to("external")
                elif case == "rules_file":
                    (project / ".claude/rules").unlink()
                    (project / ".claude/rules").write_text("not a link")
                elif case == "index_symlink":
                    (project / "docs/rules/INDEX.md").unlink()
                    external.write_text("# External\n")
                    (project / "docs/rules/INDEX.md").symlink_to("../../external")
                elif case == "agents_symlink":
                    (project / "AGENTS.md").unlink()
                    external.write_text("keep")
                    (project / "AGENTS.md").symlink_to("external")
                elif case == "claude_symlink":
                    (project / "CLAUDE.md").unlink()
                    external.write_text("keep")
                    (project / "CLAUDE.md").symlink_to("external")
                else:
                    (project / ".claude/rules").unlink()
                    (project / ".claude").rmdir()
                    external.mkdir()
                    (project / ".claude").symlink_to("external")
                result = self.run_cli(project, "validate")
                self.assertNotEqual(result.returncode, 0)

    def test_init_recognizes_existing_references_without_rewriting_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            agents_content = (
                "# Existing agents\n\n"
                "See `docs/rules/INDEX.md` before starting work.\n"
            )
            claude_content = "# Existing Claude\n\n@AGENTS.md\n"
            (project / "AGENTS.md").write_text(agents_content, encoding="utf-8")
            (project / "CLAUDE.md").write_text(claude_content, encoding="utf-8")

            result = self.run_cli(project, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (project / "AGENTS.md").read_text(encoding="utf-8"), agents_content
            )
            self.assertEqual(
                (project / "CLAUDE.md").read_text(encoding="utf-8"), claude_content
            )

    def test_validate_accepts_content_based_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            self.initialize_valid_project(project)
            (project / "AGENTS.md").write_text(
                "See `docs/rules/INDEX.md` before starting work.\n",
                encoding="utf-8",
            )
            (project / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")

            result = self.run_cli(project, "validate")

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_reference_must_be_a_standalone_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            (project / "CLAUDE.md").write_text(
                "The text @AGENTS.md is only an example.\n", encoding="utf-8"
            )

            result = self.run_cli(project, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (project / "CLAUDE.md")
                .read_text(encoding="utf-8")
                .splitlines()
                .count("@AGENTS.md"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
