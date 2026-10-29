#!/usr/bin/env python3
"""hook-stop-insight-logger.sh（Insight ブロックを記録する Stop フック）の契約テスト。

フックの書き込み先はすべて ${HOME}/.claude/skills/_tracker に揃っており、
スクリプト自身の置き場所には何も書かない。それを確かめるため、本体は
リポジトリと同じ形（<root>/repo/.claude/scripts/）の一時ツリーへ複製して実行し、
HOME は一時ディレクトリの stub に差し替える。スクリプト相対で解決した場合の
書き込み先は <root>/repo/.claude/skills/_tracker になり、実リポジトリには届かない。
"""

from __future__ import annotations

import json
import os
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

HOOK_NAME = "hook-stop-insight-logger.sh"
# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
HOOK_UNDER_TEST = Path(
    os.environ.get("HOOK_STOP_INSIGHT_LOGGER_TARGET")
    or REPO_ROOT / ".claude" / "scripts" / HOOK_NAME
)

# フックが実行中に新しいエントリを作ってはならない実データの置き場所。
# 実際の Claude Code セッションが並行して追記しうるため、サイズや更新時刻は見ず、
# エントリの名前と種別だけを比べる。
REAL_DATA_DIRS = (
    REPO_ROOT / ".claude" / "skills" / "_tracker",
    REAL_HOME / ".claude" / "skills" / "_tracker",
)

MINIMAL_PATH = "/usr/bin:/bin"
RUN_DEADLINE_SECONDS = 20.0
SESSION_ID = "insight-logger-test-session"


def _working_jq() -> str | None:
    """名前ではなく挙動で判定する: JSON を実際に評価できる jq だけを使う。"""
    path = shutil.which("jq")
    if path is None:
        return None
    try:
        result = subprocess.run(
            [path, "-r", ".a"],
            input='{"a":"ok"}',
            env={"PATH": MINIMAL_PATH},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if result.returncode == 0 and result.stdout == "ok\n" else None


JQ_PATH = _working_jq()


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


def entry_names(path: Path) -> list[tuple[str, int]] | None:
    """path 配下のエントリ（相対パスと種別）。存在しなければ None。"""
    if not os.path.lexists(path):
        return None
    entries = []
    if path.is_dir() and not path.is_symlink():
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for name in sorted([*dirnames, *filenames]):
                entry = Path(dirpath) / name
                entries.append(
                    (entry.relative_to(path).as_posix(), stat.S_IFMT(entry.lstat().st_mode))
                )
    return entries


def transcript_line(role: str, text: str, *, as_blocks: bool = False) -> str:
    content = [{"type": "text", "text": text}] if as_blocks else text
    return json.dumps({"type": role, "message": {"content": content}}, ensure_ascii=False)


@dataclass
class HookRun:
    returncode: int
    stdout: str
    stderr: str


@unittest.skipUnless(JQ_PATH, "a working jq is not available")
class HookStopInsightLoggerTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="hook-stop-insight-logger-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        abort_if_unsafe_temp_root(self.root)

        # リポジトリと同じ相対配置に複製する。スクリプト相対の ../skills/_tracker は
        # この一時ツリー内の repo/.claude/skills/_tracker に解決される。
        self.repo = self.root / "repo"
        self.hook = self.repo / ".claude" / "scripts" / HOOK_NAME
        self.hook.parent.mkdir(parents=True)
        shutil.copy2(HOOK_UNDER_TEST, self.hook)
        self.script_relative_skills = self.repo / ".claude" / "skills"

        self.home = self.root / "home"
        self.workdir = self.root / "my-project"
        self.bin_dir = self.root / "bin"
        for directory in (self.home, self.workdir, self.bin_dir):
            directory.mkdir(parents=True)
        assert JQ_PATH is not None
        (self.bin_dir / "jq").symlink_to(JQ_PATH)

        self.transcript = self.root / "transcript.jsonl"
        self.tracker = self.home / ".claude" / "skills" / "_tracker"
        self.debug_log = self.tracker / "debug-stop.log"
        self.insights_log = self.tracker / "insights.jsonl"

    # -- environment -------------------------------------------------------

    def hook_env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": f"{self.bin_dir}:{MINIMAL_PATH}",
            "TMPDIR": str(self.root),
        }

    def abort_if_environment_escapes_tree(self, env: dict[str, str]) -> None:
        """フックはログを書き込むので、隔離ツリーの外を指したまま起動しない。"""
        problems = [
            f"{name}={env[name]}"
            for name in ("HOME", "TMPDIR")
            if not Path(env[name]).resolve().is_relative_to(self.root)
        ]
        if not self.hook.resolve().is_relative_to(self.root):
            problems.append(f"hook={self.hook}")
        if problems:
            sys.stderr.write("aborting: environment escapes the test tree: " + "; ".join(problems))
            sys.stderr.write("\n")
            sys.stderr.flush()
            os._exit(3)

    @contextmanager
    def real_data_unchanged(self) -> Iterator[None]:
        before = {path: entry_names(path) for path in REAL_DATA_DIRS}
        yield
        for path in REAL_DATA_DIRS:
            self.assertEqual(entry_names(path), before[path], f"{path} changed during the run")

    # -- running -----------------------------------------------------------

    def payload(self, **overrides: object) -> str:
        data: dict[str, object] = {"session_id": SESSION_ID, "cwd": str(self.workdir)}
        data.update(overrides)
        return json.dumps(data, ensure_ascii=False)

    def write_transcript(self, lines: list[str]) -> None:
        self.transcript.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def run_hook(self, stdin: str) -> HookRun:
        env = self.hook_env()
        self.abort_if_environment_escapes_tree(env)
        with self.real_data_unchanged():
            try:
                result = subprocess.run(
                    [str(self.hook)],
                    input=stdin.encode("utf-8"),
                    capture_output=True,
                    env=env,
                    cwd=str(self.workdir),
                    timeout=RUN_DEADLINE_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail(f"the hook did not finish within {RUN_DEADLINE_SECONDS} seconds")
        return HookRun(
            result.returncode,
            result.stdout.decode("utf-8", "replace"),
            result.stderr.decode("utf-8", "replace"),
        )

    # -- assertions --------------------------------------------------------

    def assert_finished_silently(self, run: HookRun) -> None:
        # Stop フックの exit 2 は停止をブロックするため、常に 0 で終える。
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stdout, "", "the hook must not write to stdout")
        self.assertEqual(run.stderr, "", "the hook must not write to stderr")

    def assert_nothing_written_beside_the_hook(self) -> None:
        self.assertFalse(
            os.path.lexists(self.script_relative_skills),
            f"created next to the hook: {entry_names(self.script_relative_skills)}",
        )
        self.assertEqual(
            entry_names(self.repo),
            [
                (".claude", stat.S_IFDIR),
                (".claude/scripts", stat.S_IFDIR),
                (f".claude/scripts/{HOOK_NAME}", stat.S_IFREG),
            ],
        )

    def debug_lines(self) -> list[str]:
        self.assertTrue(self.debug_log.is_file(), f"missing debug log: {self.debug_log}")
        return self.debug_log.read_text(encoding="utf-8").splitlines()


class TestDebugLogLocation(HookStopInsightLoggerTestBase):
    def test_debug_log_is_written_under_home_tracker_when_payload_has_no_transcript(self) -> None:
        # Act
        run = self.run_hook(self.payload())

        # Assert
        self.assert_finished_silently(run)
        lines = self.debug_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(
            lines[0],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z called: keys=cwd,session_id "
            r"transcript_path=\(none\)$",
        )

    def test_nothing_is_created_beside_the_hook_when_payload_has_no_transcript(self) -> None:
        # Act
        run = self.run_hook(self.payload())

        # Assert
        self.assert_finished_silently(run)
        self.assert_nothing_written_beside_the_hook()

    def test_debug_log_is_appended_on_each_invocation(self) -> None:
        # Act
        first = self.run_hook(self.payload())
        second = self.run_hook(self.payload(transcript_path=str(self.root / "missing.jsonl")))

        # Assert
        self.assert_finished_silently(first)
        self.assert_finished_silently(second)
        lines = self.debug_lines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].endswith("transcript_path=(none)"), lines[0])
        self.assertTrue(
            lines[1].endswith(f"transcript_path={self.root / 'missing.jsonl'}"), lines[1]
        )

    def test_existing_debug_log_under_home_is_kept_and_appended(self) -> None:
        # Arrange: 手で残した過去のログ（CRLF・末尾改行あり）を保持したまま追記する
        self.tracker.mkdir(parents=True)
        previous = b"old line from another run\r\n"
        self.debug_log.write_bytes(previous)

        # Act
        run = self.run_hook(self.payload())

        # Assert
        self.assert_finished_silently(run)
        content = self.debug_log.read_bytes()
        self.assertTrue(content.startswith(previous), content)
        self.assertEqual(content[len(previous):].count(b"\n"), 1, content)


class TestInsightRecording(HookStopInsightLoggerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.write_transcript(
            [
                transcript_line("user", "質問"),
                transcript_line(
                    "assistant",
                    "前置き\n`★ Insight ─────`\n記録すべき知見\n`─────`\n後書き",
                    as_blocks=True,
                ),
            ]
        )

    def test_insight_is_recorded_under_home_tracker(self) -> None:
        # Act
        run = self.run_hook(self.payload(transcript_path=str(self.transcript)))

        # Assert
        self.assert_finished_silently(run)
        records = [
            json.loads(line)
            for line in self.insights_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["session_id"], SESSION_ID)
        self.assertEqual(records[0]["cwd"], str(self.workdir))
        self.assertEqual(records[0]["insights"], ["記録すべき知見"])

    def test_insight_and_debug_log_share_the_home_tracker_directory(self) -> None:
        # Act
        run = self.run_hook(self.payload(transcript_path=str(self.transcript)))

        # Assert
        self.assert_finished_silently(run)
        self.assertEqual(
            entry_names(self.tracker),
            [("debug-stop.log", stat.S_IFREG), ("insights.jsonl", stat.S_IFREG)],
        )
        self.assertEqual(len(self.debug_lines()), 1)
        self.assert_nothing_written_beside_the_hook()


class TestHarness(unittest.TestCase):
    def test_entry_names_detects_a_directory_that_did_not_exist_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            created = Path(tmp) / "skills" / "_tracker"

            # Arrange
            before = entry_names(created)

            # Act
            created.mkdir(parents=True)
            (created / "debug-stop.log").write_text("x", encoding="utf-8")
            after = entry_names(created)

            # Assert
            self.assertIsNone(before)
            self.assertEqual(after, [("debug-stop.log", stat.S_IFREG)])


if __name__ == "__main__":
    unittest.main()
