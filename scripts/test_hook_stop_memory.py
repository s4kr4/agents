#!/usr/bin/env python3
"""hook-stop-memory.sh（セッションを記録する Stop フック）の契約テスト。

共有メモリの CLI は ~/.agents には無く、位置は環境変数 MEMORY_MCP_PATH だけで
決まる。テストは偽の CLI ツリー（run-python.sh と memory.py）を一時ディレクトリ
に作り、HOME・TMPDIR も一時ディレクトリへ差し替えて実行する。実ストアの
モジュールは import せず、標準ライブラリだけで動く。
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
import textwrap
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
REAL_HOME = Path.home().resolve()

HOOK_NAME = "hook-stop-memory.sh"
# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
HOOK_UNDER_TEST = Path(
    os.environ.get("HOOK_STOP_MEMORY_TARGET") or REPO_ROOT / ".claude" / "scripts" / HOOK_NAME
)

# フックが実行中に書き換えてはならない実データ。
REAL_DATA_DIRS = (
    REPO_ROOT / "memory" / "local",
    REPO_ROOT / "memory" / "vault",
    REAL_HOME / "worktrees" / "github.com" / "s4kr4" / "memory-mcp" / "local",
    REAL_HOME / "worktrees" / "github.com" / "s4kr4" / "memory-mcp" / "vault",
    REAL_HOME / ".cache" / "llm-memory",
)

MINIMAL_PATH = "/usr/bin:/bin"
# スタブの呼び出し記録の区切り。引数自体は NUL 区切りなので改行を含む値も壊れない。
RECORD_SEPARATOR = "\x1e"
RUN_DEADLINE_SECONDS = 30.0

SESSION_ID = "claude-test-session"
JQ_PATH = shutil.which("jq")

# CLI の位置は MEMORY_MCP_PATH だけで決まる。既定値やフォールバック探索を表す表現。
FALLBACK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("memory-mcp の clone のハードコード", r"worktrees/github\.com/s4kr4/memory-mcp"),
    ("~/.agents/memory への参照", r"\.agents/memory"),
    ("MEMORY_MCP_PATH の既定値", r"\$\{MEMORY_MCP_PATH:[-=][^}]"),
)


def shell_code_lines(script: str) -> list[tuple[int, str]]:
    """shebang と行まるごとのコメントを除いたシェルスクリプトの行。"""
    return [
        (number, line)
        for number, line in enumerate(script.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


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


def parse_calls(log: Path) -> list[list[str]]:
    """スタブが記録した引数ベクタを呼び出し順に返す。"""
    if not log.exists():
        return []
    calls = []
    for record in log.read_text(encoding="utf-8").split(RECORD_SEPARATOR):
        if not record:
            continue
        arguments = record.split("\0")
        if arguments and arguments[-1] == "":
            arguments.pop()
        calls.append(arguments)
    return calls


def option_value(call: list[str], name: str) -> str | None:
    for index, argument in enumerate(call):
        if argument == name and index + 1 < len(call):
            return call[index + 1]
    return None


def transcript_line(role: str, text: str, *, as_blocks: bool = False) -> str:
    content = [{"type": "text", "text": text}] if as_blocks else text
    return json.dumps({"type": role, "message": {"content": content}}, ensure_ascii=False)


def collapse(text: str) -> str:
    return " ".join(text.split())


@dataclass
class HookRun:
    returncode: int
    stdout: str
    stderr: str


class TestHookScriptFile(unittest.TestCase):
    """スクリプト本体そのものに対する不変条件。"""

    def setUp(self):
        self.assertTrue(HOOK_UNDER_TEST.is_file(), f"missing hook script: {HOOK_UNDER_TEST}")
        self.source = HOOK_UNDER_TEST.read_text(encoding="utf-8")

    def test_hook_is_an_executable_bash_script(self):
        self.assertTrue(os.access(HOOK_UNDER_TEST, os.X_OK), "hook script must be executable")
        first_line = self.source.splitlines()[0]
        self.assertTrue(first_line.startswith("#!"), first_line)
        self.assertIn("bash", first_line)

    def test_hook_has_no_default_or_fallback_location_for_the_cli(self):
        # Arrange
        lines = shell_code_lines(self.source)

        # Act
        findings = [
            f"line {number} ({label}): {line.strip()}"
            for number, line in lines
            for label, pattern in FALLBACK_PATTERNS
            if re.search(pattern, line)
        ]

        # Assert
        self.assertEqual(findings, [])


@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class HookStopMemoryTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hook-stop-memory-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        abort_if_unsafe_temp_root(self.root)

        # フックは自分の隣ではなく MEMORY_MCP_PATH を見る。それを確かめるため、
        # 本体はリポジトリとは無関係な場所に複製して実行する。
        self.hook = self.root / "repo" / ".claude" / "scripts" / HOOK_NAME
        self.hook.parent.mkdir(parents=True)
        shutil.copy2(HOOK_UNDER_TEST, self.hook)

        self.home = self.root / "home"
        self.tmpdir = self.root / "tmp"
        self.workdir = self.root / "my-project"
        self.bin_dir = self.root / "bin"
        self.vault = self.root / "vault"
        self.local_dir = self.root / "local"
        self.queue_dir = self.root / "queue"
        self.config = self.root / "config" / "empty-config.toml"
        for directory in (self.home, self.tmpdir, self.workdir, self.bin_dir, self.config.parent):
            directory.mkdir(parents=True)
        self.config.write_text("", encoding="utf-8")
        assert JQ_PATH is not None
        (self.bin_dir / "jq").symlink_to(JQ_PATH)

        self.call_log = self.root / "cli-calls.log"
        self.decoy_log = self.root / "decoy-calls.log"
        self.cli_tree = self.make_cli_tree("memory-mcp")
        self.python_stub = self.make_interpreter_stub("python-stub", "exit 0")
        self.transcript = self.root / "transcript.jsonl"

    # -- fixtures ----------------------------------------------------------

    def write_executable(self, path: Path, body: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def make_cli_tree(self, name: str) -> Path:
        """MEMORY_MCP_PATH が指しうる、正しい形の CLI ツリー。"""
        tree = self.root / name
        tree.mkdir(parents=True)
        (tree / "memory.py").write_text("", encoding="utf-8")
        self.write_executable(
            tree / "run-python.sh",
            """\
            exec "$LLM_MEMORY_PYTHON" "$0" "$@"
            """,
        )
        return tree

    def make_interpreter_stub(self, name: str, tail: str) -> Path:
        """引数ベクタを記録してから ``tail`` を実行する、インタプリタの代役。

        run-python.sh は自分のパスを第 1 引数として渡すので、記録には
        「どの run-python.sh が、どの memory.py を、どの引数で動かしたか」が残る。
        """
        return self.write_executable(
            self.root / "stubs" / name,
            """\
            {
              printf '%s\\0' "$@"
              printf '\\036'
            } >>"$TEST_CLI_CALL_LOG"
            shift
            """
            + textwrap.dedent(tail),
        )

    def use_interpreter_that_only_accepts_queueing(self) -> None:
        """直接書き込みは失敗し queue-session だけ通る、ストア障害時の姿。"""
        self.python_stub = self.make_interpreter_stub(
            "python-stub-queue-only",
            """\
            for argument in "$@"; do
              if [ "$argument" = "queue-session" ]; then
                exit 0
              fi
            done
            exit 1
            """,
        )

    def use_interpreter_that_always_fails(self) -> None:
        self.python_stub = self.make_interpreter_stub("python-stub-failing", "exit 1")

    def write_transcript(self, lines: list[str]) -> None:
        self.transcript.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def plant_decoys(self) -> dict[str, Path]:
        """MEMORY_MCP_PATH 以外の場所に置いた、呼ばれてはならない CLI ツリー。"""
        locations = {
            "beside the hook": self.hook.parent,
            "under the home directory": self.home / ".agents" / "memory",
            "in the memory-mcp clone": (
                self.home / "worktrees" / "github.com" / "s4kr4" / "memory-mcp"
            ),
        }
        for decoy in locations.values():
            decoy.mkdir(parents=True, exist_ok=True)
            (decoy / "memory.py").write_text("", encoding="utf-8")
            self.write_executable(
                decoy / "run-python.sh",
                """\
                {
                  printf '%s\\0' "$0" "$@"
                  printf '\\036'
                } >>"$TEST_DECOY_CALL_LOG"
                exit 0
                """,
            )
        return locations

    def unusable_memory_mcp_paths(self) -> list[tuple[str, str | None]]:
        """MEMORY_MCP_PATH として受け付けてはならない値。"""
        broken = self.root / "broken"
        broken.mkdir()

        # 相対パスは、作業ディレクトリから解決すると正しいツリーになる形で置く。
        # 「存在するか」だけを見る実装がここで露見する。
        relative_tree = self.workdir / "relative" / "memory-mcp"
        relative_tree.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.cli_tree, relative_tree)

        # チルダも同様に、展開されていれば有効になる位置へ実体を置く。
        shutil.copytree(self.cli_tree, self.home / "mcp-tree")

        regular_file = broken / "not-a-directory"
        regular_file.write_text("", encoding="utf-8")

        without_cli = broken / "without-memory-py"
        without_cli.mkdir()
        self.write_executable(without_cli / "run-python.sh", 'exec "$LLM_MEMORY_PYTHON" "$@"\n')

        cli_is_a_directory = broken / "memory-py-is-a-directory"
        cli_is_a_directory.mkdir()
        (cli_is_a_directory / "memory.py").mkdir()
        self.write_executable(
            cli_is_a_directory / "run-python.sh", 'exec "$LLM_MEMORY_PYTHON" "$@"\n'
        )

        without_runner = broken / "without-run-python"
        without_runner.mkdir()
        (without_runner / "memory.py").write_text("", encoding="utf-8")

        runner_not_executable = broken / "run-python-not-executable"
        runner_not_executable.mkdir()
        (runner_not_executable / "memory.py").write_text("", encoding="utf-8")
        (runner_not_executable / "run-python.sh").write_text(
            '#!/bin/bash\nexec "$LLM_MEMORY_PYTHON" "$@"\n', encoding="utf-8"
        )
        (runner_not_executable / "run-python.sh").chmod(0o644)

        # memory.py がディレクトリの場合と対になる形。ディレクトリには実行
        # ビットが立つため、-x だけを見る実装はここを通してしまう。
        runner_is_a_directory = broken / "run-python-is-a-directory"
        runner_is_a_directory.mkdir()
        (runner_is_a_directory / "memory.py").write_text("", encoding="utf-8")
        (runner_is_a_directory / "run-python.sh").mkdir()

        return [
            ("unset", None),
            ("empty", ""),
            ("whitespace only", "   "),
            ("relative path", str(relative_tree.relative_to(self.workdir))),
            ("unexpanded tilde", "~/mcp-tree"),
            ("nonexistent directory", str(broken / "missing")),
            ("regular file", str(regular_file)),
            ("without memory.py", str(without_cli)),
            ("memory.py is a directory", str(cli_is_a_directory)),
            ("without run-python.sh", str(without_runner)),
            ("run-python.sh is not executable", str(runner_not_executable)),
            ("run-python.sh is a directory", str(runner_is_a_directory)),
        ]

    # -- environment -------------------------------------------------------

    def hook_env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin_dir}:{MINIMAL_PATH}",
            "TMPDIR": str(self.tmpdir),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "MEMORY_MCP_PATH": str(self.cli_tree),
            "TEST_CLI_CALL_LOG": str(self.call_log),
            "TEST_DECOY_CALL_LOG": str(self.decoy_log),
            "LLM_MEMORY_PYTHON": str(self.python_stub),
            "LLM_MEMORY_VAULT": str(self.vault),
            "LLM_MEMORY_LOCAL_DIR": str(self.local_dir),
            "LLM_MEMORY_QUEUE_DIR": str(self.queue_dir),
            "LLM_MEMORY_CONFIG": str(self.config),
        }
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def abort_if_environment_escapes_tree(self, env: dict[str, str], cwd: Path) -> None:
        """フックは記憶を書き込むので、隔離ツリーの外を指したまま起動しない。"""
        checked = (
            "HOME",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "LLM_MEMORY_PYTHON",
            "LLM_MEMORY_VAULT",
            "LLM_MEMORY_LOCAL_DIR",
            "LLM_MEMORY_QUEUE_DIR",
            "LLM_MEMORY_CONFIG",
            "TEST_CLI_CALL_LOG",
            "TEST_DECOY_CALL_LOG",
        )
        problems = [
            f"{name}={env[name]}"
            for name in (*checked,)
            if name in env and not Path(env[name]).resolve().is_relative_to(self.root)
        ]
        if not Path(cwd).resolve().is_relative_to(self.root):
            problems.append(f"cwd={cwd}")
        # MEMORY_MCP_PATH は不正値のテストで存在しない値も取るため、実在する
        # ディレクトリを指しているときだけツリー内であることを求める。
        candidate = env.get("MEMORY_MCP_PATH", "")
        if candidate and os.path.isdir(candidate):
            if not Path(candidate).resolve().is_relative_to(self.root):
                problems.append(f"MEMORY_MCP_PATH={candidate}")
        if env.get("HOME") and Path(env["HOME"]).resolve() == REAL_HOME:
            problems.append("HOME is the real home directory")
        if problems:
            sys.stderr.write("aborting: environment escapes the test tree: " + "; ".join(problems))
            sys.stderr.write("\n")
            sys.stderr.flush()
            os._exit(3)

    @contextmanager
    def real_data_unchanged(self) -> Iterator[None]:
        before = {path: snapshot_tree(path) for path in REAL_DATA_DIRS}
        yield
        for path in REAL_DATA_DIRS:
            self.assertEqual(snapshot_tree(path), before[path], f"{path} changed during the run")

    # -- running -----------------------------------------------------------

    def payload(self, **overrides: object) -> str:
        data: dict[str, object] = {
            "session_id": SESSION_ID,
            "hook_event_name": "Stop",
            "cwd": str(self.workdir),
            "transcript_path": str(self.transcript),
        }
        data.update(overrides)
        return json.dumps(data, ensure_ascii=False)

    def run_hook(
        self,
        *,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        hook: Path | None = None,
    ) -> HookRun:
        if env is None:
            env = self.hook_env()
        if stdin is None:
            stdin = self.payload()
        if hook is None:
            hook = self.hook
        self.abort_if_environment_escapes_tree(env, self.workdir)
        self.assertTrue(hook.is_file(), f"hook script is missing: {hook}")
        with self.real_data_unchanged():
            process = subprocess.Popen(
                [str(hook)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(self.workdir),
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

    # -- assertions --------------------------------------------------------

    def cli_calls(self) -> list[list[str]]:
        return parse_calls(self.call_log)

    def decoy_calls(self) -> list[list[str]]:
        return parse_calls(self.decoy_log)

    def assert_finished_silently(self, run: HookRun) -> None:
        # Stop フックの exit 2 は停止をブロックするため、失敗しても 0 で終える。
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stdout, "", "the hook must not write to stdout")
        self.assertEqual(run.stderr, "", "the hook must not write to stderr")

    def assert_used_cli_under(self, call: list[str], tree: Path, subcommand: str) -> None:
        self.assertTrue(call, "the CLI was not invoked at all")
        self.assertEqual(Path(call[0]), tree / "run-python.sh")
        self.assertEqual(Path(call[1]), tree / "memory.py")
        self.assertEqual(call[2:3], [subcommand])


class TestHookRecordsSession(HookStopMemoryTestBase):
    def test_records_the_session_through_the_cli_under_memory_mcp_path(self):
        # Arrange
        user_text = "最初の行\n二行目"
        assistant_text = "アシスタントの返答"
        self.write_transcript(
            [
                transcript_line("user", user_text),
                transcript_line("assistant", assistant_text, as_blocks=True),
            ]
        )

        # Act
        run = self.run_hook()

        # Assert
        self.assert_finished_silently(run)
        calls = self.cli_calls()
        self.assertEqual(
            [call[2] for call in calls],
            ["start-session", "append-event", "append-event", "end-session"],
            calls,
        )
        for call in calls:
            self.assertEqual(Path(call[0]), self.cli_tree / "run-python.sh")
            self.assertEqual(Path(call[1]), self.cli_tree / "memory.py")
            self.assertEqual(option_value(call, "--session-id"), SESSION_ID)
        # end-session はセッションだけを指す。他の呼び出しは所有者も伴う。
        for call in calls[:3]:
            self.assertEqual(option_value(call, "--client"), "claude-code")
            self.assertEqual(option_value(call, "--user-id"), "default")
            self.assertEqual(option_value(call, "--project-id"), self.workdir.name)
        self.assertEqual(option_value(calls[1], "--role"), "user")
        self.assertEqual(option_value(calls[1], "--content"), user_text)
        self.assertEqual(option_value(calls[2], "--role"), "assistant")
        self.assertEqual(option_value(calls[2], "--content"), assistant_text)
        self.assertEqual(
            option_value(calls[3], "--summary"),
            f"user: {collapse(user_text)} / assistant: {assistant_text}",
        )
        self.assertIn("--append-summary-event", calls[3])
        self.assertIn("--extract", calls[3])
        self.assertIn("--consolidate", calls[3])

    def test_accepts_a_trailing_slash_in_memory_mcp_path(self):
        # Arrange
        self.write_transcript([transcript_line("user", "ユーザーの発言")])

        # Act
        run = self.run_hook(env=self.hook_env(MEMORY_MCP_PATH=f"{self.cli_tree}/"))

        # Assert
        self.assert_finished_silently(run)
        calls = self.cli_calls()
        self.assertTrue(calls, "the CLI was not invoked at all")
        for call in calls:
            self.assertEqual(Path(call[1]), self.cli_tree / "memory.py")

    def test_accepts_a_memory_mcp_path_containing_spaces(self):
        # Arrange
        spaced = self.make_cli_tree("memory mcp with spaces")
        self.write_transcript([transcript_line("user", "ユーザーの発言")])

        # Act
        run = self.run_hook(env=self.hook_env(MEMORY_MCP_PATH=str(spaced)))

        # Assert
        self.assert_finished_silently(run)
        calls = self.cli_calls()
        self.assertTrue(calls, "the CLI was not invoked at all")
        for call in calls:
            self.assertEqual(Path(call[1]), spaced / "memory.py")

    def test_ignores_cli_trees_beside_the_hook_and_under_the_home_directory(self):
        # Arrange
        decoys = self.plant_decoys()
        self.write_transcript([transcript_line("user", "ユーザーの発言")])

        # Act
        run = self.run_hook()

        # Assert
        self.assert_finished_silently(run)
        calls = self.cli_calls()
        self.assertTrue(calls, "the CLI was not invoked at all")
        for call in calls:
            self.assertEqual(Path(call[1]), self.cli_tree / "memory.py")
        self.assertEqual(self.decoy_calls(), [], f"a decoy CLI was invoked: {decoys}")

    def test_skips_the_assistant_event_when_the_transcript_has_no_assistant_message(self):
        # Arrange
        self.write_transcript([transcript_line("user", "ユーザーだけの発言")])

        # Act
        run = self.run_hook()

        # Assert
        self.assert_finished_silently(run)
        calls = self.cli_calls()
        self.assertEqual(
            [call[2] for call in calls], ["start-session", "append-event", "end-session"], calls
        )
        self.assertEqual(option_value(calls[1], "--role"), "user")

    def test_falls_back_to_queueing_when_the_direct_writes_fail(self):
        # Arrange
        self.use_interpreter_that_only_accepts_queueing()
        user_text = "ユーザーの発言"
        assistant_text = "アシスタントの返答"
        self.write_transcript(
            [
                transcript_line("user", user_text),
                transcript_line("assistant", assistant_text),
            ]
        )

        # Act
        run = self.run_hook(env=self.hook_env())

        # Assert
        self.assert_finished_silently(run)
        calls = self.cli_calls()
        self.assertTrue(calls, "the CLI was not invoked at all")
        self.assert_used_cli_under(calls[-1], self.cli_tree, "queue-session")
        self.assertEqual(option_value(calls[-1], "--session-id"), SESSION_ID)
        self.assertEqual(option_value(calls[-1], "--project-id"), self.workdir.name)
        self.assertEqual(option_value(calls[-1], "--user-content"), user_text)
        self.assertEqual(option_value(calls[-1], "--assistant-content"), assistant_text)
        self.assertEqual(
            option_value(calls[-1], "--summary"),
            f"user: {user_text} / assistant: {assistant_text}",
        )

    def test_resolves_the_cli_the_same_way_from_a_copy_in_another_directory(self):
        # Arrange
        relocated = self.root / "elsewhere" / "nested" / HOOK_NAME
        relocated.parent.mkdir(parents=True)
        shutil.copy2(self.hook, relocated)
        self.write_transcript([transcript_line("user", "ユーザーの発言")])

        # Act
        run = self.run_hook(hook=relocated)

        # Assert
        self.assert_finished_silently(run)
        calls = self.cli_calls()
        self.assertTrue(calls, "the CLI was not invoked at all")
        for call in calls:
            self.assertEqual(Path(call[1]), self.cli_tree / "memory.py")


class TestHookStaysOutOfTheWay(HookStopMemoryTestBase):
    def test_does_nothing_when_the_transcript_file_is_missing(self):
        # Arrange: transcript のパスは作らない。

        # Act
        run = self.run_hook()

        # Assert
        self.assert_finished_silently(run)
        self.assertEqual(self.cli_calls(), [])

    def test_does_nothing_when_the_payload_has_no_session_id(self):
        # Arrange
        self.write_transcript([transcript_line("user", "ユーザーの発言")])

        # Act
        run = self.run_hook(stdin=self.payload(session_id=""))

        # Assert
        self.assert_finished_silently(run)
        self.assertEqual(self.cli_calls(), [])

    def test_stays_silent_and_succeeds_when_every_cli_call_fails(self):
        # Arrange
        self.use_interpreter_that_always_fails()
        self.write_transcript([transcript_line("user", "ユーザーの発言")])

        # Act
        run = self.run_hook(env=self.hook_env())

        # Assert
        self.assert_finished_silently(run)

    def test_finishes_silently_without_running_any_cli_when_memory_mcp_path_is_unusable(self):
        # Arrange
        decoys = self.plant_decoys()
        self.write_transcript(
            [
                transcript_line("user", "ユーザーの発言"),
                transcript_line("assistant", "アシスタントの返答"),
            ]
        )

        for label, value in self.unusable_memory_mcp_paths():
            with self.subTest(memory_mcp_path=label):
                # Arrange
                self.call_log.write_text("", encoding="utf-8")
                self.decoy_log.write_text("", encoding="utf-8")

                # Act
                run = self.run_hook(env=self.hook_env(MEMORY_MCP_PATH=value))

                # Assert: queue へのフォールバックも含め、CLI は一度も呼ばれない。
                self.assert_finished_silently(run)
                self.assertEqual(self.cli_calls(), [])
                self.assertEqual(self.decoy_calls(), [], f"a decoy CLI was invoked: {decoys}")


if __name__ == "__main__":
    unittest.main()
