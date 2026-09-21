#!/usr/bin/env python3
"""hook-pre-boundary.sh（書き込み先を検査する PreToolUse フック）の契約テスト。

フックはプロジェクトルートを CLAUDE_PROJECT_DIR（未設定なら作業ディレクトリ）から、
スクラッチパッドの位置を TMPDIR・実行ユーザーの UID・PreToolUse ペイロードの
session_id から決める。テストは HOME・TMPDIR・CLAUDE_PROJECT_DIR をすべて一時
ディレクトリへ差し替え、環境を継承せずに実行する。フックは読み出し専用だが、
実環境が変化していないことを実行の前後で比較して確かめる。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
REAL_HOME = Path.home().resolve()

HOOK_NAME = "hook-pre-boundary.sh"
# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
HOOK_UNDER_TEST = Path(
    os.environ.get("HOOK_PRE_BOUNDARY_TARGET") or REPO_ROOT / ".claude" / "scripts" / HOOK_NAME
)

# フックが実行中に書き換えてはならない実データ。
REAL_DATA_PATHS = (
    REPO_ROOT / ".claude" / "scripts",
    REAL_HOME / ".claude" / "settings.json",
)

MINIMAL_PATH = "/usr/bin:/bin"
RUN_DEADLINE_SECONDS = 30.0

# 実際のスクラッチパッドと同じ形のセッション ID。
SESSION_ID = "d1373fcb-6a5f-4a04-8ea6-7925d5fd7cc3"
OTHER_SESSION_ID = "0f2a91cc-5b3e-4d17-9a60-2c8e14b7f503"
PROJECT_SLUG = "-home-s4kr4--agents"

CURRENT_UID = os.getuid()
# 実行ユーザーとは必ず異なる UID。
FOREIGN_UID = CURRENT_UID + 1

JQ_PATH = shutil.which("jq")
REALPATH_PATH = shutil.which("realpath")

# フックの出力に現れてはならない文字（C0 制御文字）。
CONTROL_CHARACTERS = frozenset(chr(code) for code in range(0x20))


def abort_if_unsafe_temp_root(root: Path) -> None:
    """隔離ツリーが実データと重なりうる場合は 1 ケースではなく実行全体を止める。"""
    root = root.resolve()
    temp_base = Path(tempfile.gettempdir()).resolve()
    problems = []
    if root == temp_base or not root.is_relative_to(temp_base):
        problems.append(f"{root} is not inside {temp_base}")
    if root.is_relative_to(REPO_ROOT) or REPO_ROOT.is_relative_to(root):
        problems.append(f"{root} overlaps the repository {REPO_ROOT}")
    if root.is_relative_to(REAL_HOME) or REAL_HOME.is_relative_to(root):
        problems.append(f"{root} overlaps the real home {REAL_HOME}")
    if problems:
        sys.stderr.write("aborting: unsafe test tree: " + "; ".join(problems) + "\n")
        sys.stderr.flush()
        os._exit(3)


def snapshot_tree(path: Path) -> list[tuple[str, int, int, int]] | None:
    if not os.path.lexists(path):
        return None
    root_stat = path.lstat()
    entries = [(".", stat.S_IFMT(root_stat.st_mode), root_stat.st_size, root_stat.st_mtime_ns)]
    if path.is_dir() and not path.is_symlink():
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for name in sorted([*dirnames, *filenames]):
                entry = Path(dirpath) / name
                entry_stat = entry.lstat()
                entries.append(
                    (
                        entry.relative_to(path).as_posix(),
                        stat.S_IFMT(entry_stat.st_mode),
                        entry_stat.st_size,
                        entry_stat.st_mtime_ns,
                    )
                )
    return entries


@dataclass
class HookRun:
    returncode: int
    stdout: str
    stderr: str


class TestHookScriptFile(unittest.TestCase):
    """スクリプト本体そのものに対する不変条件。

    テストは実行ユーザーと同じ UID でしか走らせられないため、UID を固定値で
    書いた実装は実行時の判定だけでは検出できない。
    """

    def test_hook_does_not_hardcode_a_numeric_uid(self):
        # Arrange
        source = HOOK_UNDER_TEST.read_text(encoding="utf-8")
        lines = [
            (number, line)
            for number, line in enumerate(source.splitlines(), start=1)
            if line.strip() and not line.lstrip().startswith("#")
        ]

        # Act
        findings = [
            f"line {number}: {line.strip()}"
            for number, line in lines
            if re.search(r"claude-[0-9]", line)
        ]

        # Assert
        self.assertEqual(findings, [])


@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
@unittest.skipUnless(REALPATH_PATH, "realpath is not on PATH")
class HookPreBoundaryTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hook-pre-boundary-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        abort_if_unsafe_temp_root(self.root)

        # フックが自分の位置ではなく渡された環境を見ることを確かめるため、
        # 本体はリポジトリとは無関係な場所に複製して実行する。
        self.hook = self.root / "copy" / HOOK_NAME
        self.hook.parent.mkdir(parents=True)
        shutil.copy2(HOOK_UNDER_TEST, self.hook)

        self.home = self.root / "home"
        self.tmpdir = self.root / "tmp"
        self.project = self.root / "project"
        self.elsewhere = self.root / "elsewhere"
        self.outside = self.root / "outside"
        self.bin_dir = self.root / "bin"
        # 親ディレクトリごと存在しないプロジェクトルート。
        self.missing_project = self.root / "missing" / "project"

        self.skills = self.home / ".claude" / "skills"
        self.memory = self.home / ".claude" / "projects" / PROJECT_SLUG / "memory"

        for directory in (
            self.home / "other",
            self.tmpdir,
            self.project / "docs",
            self.elsewhere,
            self.outside,
            self.bin_dir,
            self.skills,
            self.memory,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        assert JQ_PATH is not None
        (self.bin_dir / "jq").symlink_to(JQ_PATH)

        self.scratchpad = self.scratchpad_for(SESSION_ID)
        self.scratchpad.mkdir(parents=True)
        # スクラッチパッドの外を指すシンボリックリンク。
        (self.scratchpad / "escape").symlink_to(self.outside)

    # -- fixtures ----------------------------------------------------------

    def scratchpad_for(self, session_id: str, *, uid: int | None = None) -> Path:
        base = self.tmpdir / f"claude-{CURRENT_UID if uid is None else uid}"
        return base / PROJECT_SLUG / session_id / "scratchpad"

    # -- environment -------------------------------------------------------

    def hook_env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin_dir}:{MINIMAL_PATH}",
            "TMPDIR": str(self.tmpdir),
            "CLAUDE_PROJECT_DIR": str(self.project),
        }
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def abort_if_environment_escapes_tree(self, env: dict[str, str], cwd: Path) -> None:
        """隔離ツリーの外を基準にしたままフックを起動しない。"""
        problems = []
        for name in ("HOME", "TMPDIR", "CLAUDE_PROJECT_DIR"):
            value = env.get(name)
            if value and not Path(value).resolve().is_relative_to(self.root):
                problems.append(f"{name}={value}")
        if not Path(cwd).resolve().is_relative_to(self.root):
            problems.append(f"cwd={cwd}")
        if env.get("HOME") and Path(env["HOME"]).resolve() == REAL_HOME:
            problems.append("HOME is the real home directory")
        if problems:
            sys.stderr.write("aborting: environment escapes the test tree: " + "; ".join(problems))
            sys.stderr.write("\n")
            sys.stderr.flush()
            os._exit(3)

    @contextmanager
    def real_environment_unchanged(self) -> Iterator[None]:
        before = {path: snapshot_tree(path) for path in REAL_DATA_PATHS}
        for path, entries in before.items():
            self.assertIsNotNone(entries, f"snapshot of {path} could not be collected")
        yield
        for path in REAL_DATA_PATHS:
            self.assertEqual(snapshot_tree(path), before[path], f"{path} changed during the run")

    # -- running -----------------------------------------------------------

    def payload(
        self,
        file_path: str | Path | None,
        *,
        session_id: str | None = SESSION_ID,
        omit_session_id: bool = False,
        omit_tool_input: bool = False,
    ) -> str:
        data: dict[str, object] = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "cwd": str(self.project),
        }
        if not omit_session_id:
            data["session_id"] = session_id
        if not omit_tool_input:
            tool_input: dict[str, object] = {}
            if file_path is not None:
                tool_input["file_path"] = str(file_path)
            data["tool_input"] = tool_input
        return json.dumps(data, ensure_ascii=False)

    def run_hook(
        self,
        *,
        stdin: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> HookRun:
        if env is None:
            env = self.hook_env()
        if cwd is None:
            cwd = self.project
        self.abort_if_environment_escapes_tree(env, cwd)
        self.assertTrue(self.hook.is_file(), f"hook script is missing: {self.hook}")
        with self.real_environment_unchanged():
            process = subprocess.Popen(
                [str(self.hook)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(cwd),
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(
                    stdin.encode("utf-8"), timeout=RUN_DEADLINE_SECONDS
                )
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f"the hook did not finish within {RUN_DEADLINE_SECONDS} seconds")
        return HookRun(
            process.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    def check(
        self,
        file_path: str | Path | None,
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        **payload_options: object,
    ) -> HookRun:
        stdin = self.payload(file_path, **payload_options)  # type: ignore[arg-type]
        return self.run_hook(stdin=stdin, env=env, cwd=cwd)

    # -- assertions --------------------------------------------------------

    def assert_allowed(self, run: HookRun) -> None:
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stderr, "", "the hook must not write to stderr")
        self.assertEqual(run.stdout, "", "an allowed path must produce no decision output")

    def assert_denied(self, run: HookRun) -> str:
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stderr, "", "the hook must not write to stderr")
        self.assertNotEqual(run.stdout, "", "a denied path must produce a decision")
        try:
            decision = json.loads(run.stdout)
        except json.JSONDecodeError as error:  # pragma: no cover - 失敗時の説明用
            self.fail(f"the decision is not valid JSON ({error}): {run.stdout!r}")
        output = decision.get("hookSpecificOutput")
        self.assertIsInstance(output, dict, run.stdout)
        self.assertEqual(output.get("hookEventName"), "PreToolUse", run.stdout)
        self.assertEqual(output.get("permissionDecision"), "deny", run.stdout)
        reason = output.get("permissionDecisionReason")
        self.assertIsInstance(reason, str, run.stdout)
        self.assertNotEqual(reason.strip(), "", "the deny reason must not be empty")
        return reason

    def assert_path_allowed(self, file_path: str | Path, **kwargs: object) -> None:
        self.assert_allowed(self.check(file_path, **kwargs))  # type: ignore[arg-type]

    def assert_path_denied(self, file_path: str | Path, **kwargs: object) -> str:
        return self.assert_denied(self.check(file_path, **kwargs))  # type: ignore[arg-type]


class TestProjectRoot(HookPreBoundaryTestBase):
    """プロジェクトルートは CLAUDE_PROJECT_DIR から決まり、未設定時のみ cwd を使う。"""

    def test_resolves_the_project_root_from_claude_project_dir(self):
        # Arrange: cwd はプロジェクトとは別の場所に置く。
        cases: list[tuple[str, Path, bool, dict[str, str | None]]] = [
            ("a file in the project", self.project / "docs" / "note.md", True, {}),
            ("the project root itself", self.project, True, {}),
            (
                "a trailing slash in the variable",
                self.project / "docs" / "note.md",
                True,
                {"CLAUDE_PROJECT_DIR": f"{self.project}/"},
            ),
            ("a file under the cwd", self.elsewhere / "note.md", False, {}),
            (
                "an outside file while the root does not exist",
                self.home / "other" / "note.md",
                False,
                {"CLAUDE_PROJECT_DIR": str(self.missing_project)},
            ),
            (
                "a cwd file while the root does not exist",
                self.elsewhere / "note.md",
                False,
                {"CLAUDE_PROJECT_DIR": str(self.missing_project)},
            ),
        ]

        for label, target, allowed, overrides in cases:
            with self.subTest(case=label):
                # Act
                run = self.check(target, env=self.hook_env(**overrides), cwd=self.elsewhere)

                # Assert
                if allowed:
                    self.assert_allowed(run)
                else:
                    self.assert_denied(run)

    def test_falls_back_to_the_cwd_when_claude_project_dir_is_absent(self):
        for label, value in (("unset", None), ("empty", "")):
            with self.subTest(claude_project_dir=label):
                # Arrange
                env = self.hook_env(CLAUDE_PROJECT_DIR=value)

                # Act / Assert
                self.assert_allowed(
                    self.check(self.elsewhere / "note.md", env=env, cwd=self.elsewhere)
                )
                self.assert_denied(
                    self.check(self.project / "docs" / "note.md", env=env, cwd=self.elsewhere)
                )


class TestScratchpad(HookPreBoundaryTestBase):
    """スクラッチパッドは TMPDIR・実行ユーザーの UID・session_id の 3 点で決まる。"""

    def test_allows_scratchpad_paths_of_the_current_session(self):
        # Arrange: TMPDIR が無い場合のベースは /tmp。実在しない位置を使う。
        fallback = (
            Path("/tmp") / f"claude-{CURRENT_UID}" / "-fallback-probe" / SESSION_ID / "scratchpad"
        )
        self.assertFalse(
            fallback.exists(), f"the fallback probe must not touch existing data: {fallback}"
        )
        cases: list[tuple[str, Path, dict[str, str | None]]] = [
            ("a file in the scratchpad", self.scratchpad / "note.md", {}),
            ("a nested file", self.scratchpad / "nested" / "deeper" / "note.md", {}),
            ("a path that normalizes back inside", self.scratchpad / "nested" / ".." / "n.md", {}),
            ("tmpdir unset", fallback / "note.md", {"TMPDIR": None}),
            ("tmpdir empty", fallback / "note.md", {"TMPDIR": ""}),
        ]

        for label, target, overrides in cases:
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_allowed(target, env=self.hook_env(**overrides))

    def test_denies_scratchpad_paths_of_a_session_that_does_not_match_exactly(self):
        # Arrange: session_id はパターンではなくリテラルとして突き合わせる。
        cases = {
            "another session": (SESSION_ID, OTHER_SESSION_ID),
            "path segment is longer": (SESSION_ID, SESSION_ID + "4"),
            "path segment is shorter": (SESSION_ID, SESSION_ID[:-1]),
            "session id is longer": (SESSION_ID + "4", SESSION_ID),
            "shared suffix only": ("abc-123", "0abc-123"),
            "glob star": ("*", "anything"),
            "glob bracket": ("[a-z][a-z][a-z]", "abc"),
            "regex dot": ("abc.123", "abcX123"),
            "regex without anchors": ("abc-123", "xxabc-123xx"),
        }
        for label, (session_id, segment) in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(
                    self.scratchpad_for(segment) / "note.md", session_id=session_id
                )

    def test_denies_paths_outside_the_scratchpad_of_the_current_user(self):
        # Arrange
        self.assertNotEqual(FOREIGN_UID, CURRENT_UID)
        self.assertTrue((self.scratchpad / "escape").is_symlink())
        base = self.tmpdir / f"claude-{CURRENT_UID}"
        cases = {
            "another uid": self.scratchpad_for(SESSION_ID, uid=FOREIGN_UID) / "note.md",
            "directly under tmpdir": self.tmpdir / "note.md",
            "directly under the claude base": base / "note.md",
            "only the slug level": base / PROJECT_SLUG / "note.md",
            "another temp base": Path("/var/tmp") / f"claude-{CURRENT_UID}" / SESSION_ID / "n.md",
            "through a symlink": self.scratchpad / "escape" / "note.md",
        }
        for label, target in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(target)

    def test_denies_scratchpad_paths_when_the_payload_carries_no_session_id(self):
        cases: dict[str, dict[str, object]] = {
            "key missing": {"omit_session_id": True},
            "null": {"session_id": None},
            "empty string": {"session_id": ""},
        }
        for label, options in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(self.scratchpad / "note.md", **options)


class TestExistingExceptions(HookPreBoundaryTestBase):
    """スキルと auto memory への書き込みは従来どおり通し、その隣接は通さない。"""

    def test_keeps_the_skills_and_memory_exceptions(self):
        claude = self.home / ".claude"
        cases: list[tuple[str, Path, bool]] = [
            ("the skills directory itself", self.skills, True),
            ("a skill file", self.skills / "tdd" / "SKILL.md", True),
            ("a memory file", self.memory / "MEMORY.md", True),
            ("a sibling of memory", claude / "projects" / PROJECT_SLUG / "notes" / "note.md", False),
            ("without a project segment", claude / "projects" / "memory" / "note.md", False),
            ("the settings file", claude / "settings.json", False),
        ]
        for label, target, allowed in cases:
            with self.subTest(case=label):
                # Act
                run = self.check(target, cwd=self.elsewhere)

                # Assert
                if allowed:
                    self.assert_allowed(run)
                else:
                    self.assert_denied(run)


class TestPayloadHandling(HookPreBoundaryTestBase):
    """ペイロードそのものの扱いと、拒否した理由の書式。"""

    def test_stays_silent_when_the_payload_carries_no_file_path(self):
        cases: dict[str, dict[str, object]] = {
            "tool_input missing": {"omit_tool_input": True},
            "file_path missing": {},
        }
        for label, options in cases.items():
            with self.subTest(case=label):
                # Act
                run = self.check(None, cwd=self.elsewhere, **options)

                # Assert
                self.assert_allowed(run)

    def test_denies_with_a_printable_reason_when_the_payload_cannot_be_honoured(self):
        # Arrange: 制御文字を含む名前。ソースにはリテラルで埋め込まない。
        name = "we" + chr(10) + "ir" + chr(1) + "d.md"

        # Act: 解析できないペイロードも拒否する（fail-closed）。
        self.assert_denied(self.run_hook(stdin="not json at all", cwd=self.elsewhere))
        reason = self.assert_path_denied(self.home / "other" / name, cwd=self.elsewhere)

        # Assert
        self.assertIn("weird.md", reason)
        leaked = sorted(CONTROL_CHARACTERS & set(reason))
        self.assertEqual(leaked, [], f"control characters leaked into the reason: {reason!r}")


if __name__ == "__main__":
    unittest.main()
