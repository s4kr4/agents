#!/usr/bin/env python3
"""codex-memory-*.sh ラッパーの契約テスト（パス解決と CLI 呼び出し）。

共有メモリの CLI は ~/.agents には無く、位置は環境変数 MEMORY_MCP_PATH だけで
決まる。テストはラッパーを一時ディレクトリへ複製し、偽の CLI ツリーと codex の
スタブ、一時ディレクトリの HOME/TMPDIR を与えて実行する。実ストアのモジュール
は import せず、標準ライブラリだけで動く。
"""

from __future__ import annotations

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

RUN_SCRIPT = "codex-memory-run.sh"
START_SCRIPT = "codex-memory-start.sh"
STOP_SCRIPT = "codex-memory-stop.sh"
LOG_SCRIPT = "codex-memory-log.sh"
SCRIPT_NAMES = (RUN_SCRIPT, START_SCRIPT, STOP_SCRIPT, LOG_SCRIPT)

# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
SCRIPT_SOURCE_DIR = Path(os.environ.get("CODEX_MEMORY_SCRIPTS_DIR") or SCRIPTS_DIR)

# ラッパーが実行中に書き換えてはならない実データ。
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

SESSION_ID = "codex-test-session"
CODEX_OUTPUT = "codex said something\nacross two lines"

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


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


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


@dataclass
class ScriptRun:
    returncode: int
    stdout: str
    stderr: str


class TestScriptFiles(unittest.TestCase):
    """スクリプト本体そのものに対する不変条件。"""

    def test_every_wrapper_is_an_executable_bash_script(self):
        for name in SCRIPT_NAMES:
            with self.subTest(script=name):
                script = SCRIPT_SOURCE_DIR / name

                self.assertTrue(script.is_file(), f"missing script: {script}")
                self.assertTrue(os.access(script, os.X_OK), "script must be executable")
                first_line = script.read_text(encoding="utf-8").splitlines()[0]
                self.assertTrue(first_line.startswith("#!"), first_line)
                self.assertIn("bash", first_line)

    def test_no_wrapper_has_a_default_or_fallback_location_for_the_cli(self):
        for name in SCRIPT_NAMES:
            with self.subTest(script=name):
                # Arrange
                script = SCRIPT_SOURCE_DIR / name
                self.assertTrue(script.is_file(), f"missing script: {script}")
                lines = shell_code_lines(script.read_text(encoding="utf-8"))

                # Act
                findings = [
                    f"line {number} ({label}): {line.strip()}"
                    for number, line in lines
                    for label, pattern in FALLBACK_PATTERNS
                    if re.search(pattern, line)
                ]

                # Assert
                self.assertEqual(findings, [])


class CodexScriptTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex-memory-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        abort_if_unsafe_temp_root(self.root)

        # ラッパーは自分の隣ではなく MEMORY_MCP_PATH に CLI を探す。それを
        # 確かめるため、リポジトリとは無関係な場所に複製して実行する。
        self.repo_root = self.root / "repo"
        self.scripts_dir = self.repo_root / "scripts"
        self.scripts_dir.mkdir(parents=True)
        for name in SCRIPT_NAMES:
            source = SCRIPT_SOURCE_DIR / name
            self.assertTrue(source.is_file(), f"missing script: {source}")
            shutil.copy2(source, self.scripts_dir / name)

        self.home = self.root / "home"
        self.tmpdir = self.root / "tmp"
        self.workdir = self.root / "work"
        self.bin_dir = self.root / "bin"
        self.vault = self.root / "vault"
        self.local_dir = self.root / "local"
        self.queue_dir = self.root / "queue"
        self.config = self.root / "config" / "empty-config.toml"
        for directory in (self.home, self.tmpdir, self.workdir, self.bin_dir, self.config.parent):
            directory.mkdir(parents=True)
        self.config.write_text("", encoding="utf-8")

        self.call_log = self.root / "cli-calls.log"
        self.decoy_log = self.root / "decoy-calls.log"
        self.codex_log = self.root / "codex-calls.log"
        self.cli_tree = self.make_cli_tree("memory-mcp")
        self.python_stub = self.make_recording_stub("python-stub")
        self.codex_stub = self.bin_dir / "codex"
        self.write_executable(
            self.codex_stub,
            f"""\
            {{
              printf '%s\\0' "$@"
              printf '\\036'
            }} >>"$TEST_CODEX_CALL_LOG"
            printf '%s\\n' {shell_quote(CODEX_OUTPUT)}
            """,
        )

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

    def make_recording_stub(self, name: str) -> Path:
        """引数ベクタをログへ追記して成功する、インタプリタの代役。

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
            exit 0
            """,
        )

    def plant_decoys(self) -> dict[str, Path]:
        """MEMORY_MCP_PATH 以外の場所に置いた、呼ばれてはならない CLI ツリー。

        ラッパーの隣・旧構成の ../memory・HOME 配下の 2 か所を覆う。``../memory``
        には旧 start/stop ラッパーのおとりも置き、run.sh が親を登らないことを
        確かめる。
        """
        locations = {
            "beside the wrappers": self.scripts_dir,
            "in the old memory directory": self.repo_root / "memory",
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
        old_layout = locations["in the old memory directory"]
        for name in (START_SCRIPT, STOP_SCRIPT, LOG_SCRIPT):
            self.write_executable(
                old_layout / name,
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

    def script_env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin_dir}:{MINIMAL_PATH}",
            "TMPDIR": str(self.tmpdir),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "MEMORY_MCP_PATH": str(self.cli_tree),
            "TEST_CLI_CALL_LOG": str(self.call_log),
            "TEST_DECOY_CALL_LOG": str(self.decoy_log),
            "TEST_CODEX_CALL_LOG": str(self.codex_log),
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
        """ラッパーはファイルを作って消すので、隔離ツリーの外を指したまま起動しない。"""
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
            "TEST_CODEX_CALL_LOG",
        )
        problems = [
            f"{name}={env[name]}"
            for name in checked
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

    def run_script(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin: str = "",
    ) -> ScriptRun:
        if env is None:
            env = self.script_env()
        if cwd is None:
            cwd = self.workdir
        self.abort_if_environment_escapes_tree(env, cwd)
        tmpdir_before = sorted(os.listdir(self.tmpdir))
        with self.real_data_unchanged():
            process = subprocess.Popen(
                argv,
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
                self.fail(f"{argv[0]} did not finish within {RUN_DEADLINE_SECONDS} seconds")
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)), tmpdir_before, "script left temporary files in TMPDIR"
        )
        return ScriptRun(
            process.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    # -- assertions --------------------------------------------------------

    def assert_succeeded(self, run: ScriptRun) -> None:
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stderr, "", "script must not write to stderr")

    def cli_calls(self) -> list[list[str]]:
        return parse_calls(self.call_log)

    def decoy_calls(self) -> list[list[str]]:
        return parse_calls(self.decoy_log)

    def codex_calls(self) -> list[list[str]]:
        return parse_calls(self.codex_log)

    def assert_used_cli_under(self, call: list[str], tree: Path, subcommand: str) -> None:
        """CLI が MEMORY_MCP_PATH の run-python.sh と memory.py で動いたこと。"""
        self.assertTrue(call, "the CLI was not invoked at all")
        self.assertEqual(Path(call[0]), tree / "run-python.sh")
        self.assertEqual(Path(call[1]), tree / "memory.py")
        self.assertEqual(call[2:3], [subcommand])

    def assert_refused_for_memory_mcp_path(self, run: ScriptRun) -> None:
        """環境変数が使えないときの共通の契約: exit 1 と、変数名を含むエラー。"""
        self.assertEqual(run.returncode, 1, f"stdout: {run.stdout!r} stderr: {run.stderr!r}")
        self.assertIn("MEMORY_MCP_PATH", run.stderr)
        self.assertEqual(self.cli_calls(), [])
        self.assertEqual(self.decoy_calls(), [])


class TestCodexMemoryStart(CodexScriptTestBase):
    def test_starts_the_session_through_the_cli_under_memory_mcp_path(self):
        # Act
        run = self.run_script([str(self.scripts_dir / START_SCRIPT), SESSION_ID])

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "start-session")
        self.assertEqual(option_value(calls[0], "--session-id"), SESSION_ID)
        self.assertEqual(option_value(calls[0], "--client"), "codex")
        self.assertEqual(option_value(calls[0], "--user-id"), "default")
        self.assertEqual(option_value(calls[0], "--project-id"), self.workdir.name)

    def test_ignores_cli_trees_beside_the_script_and_under_the_home_directory(self):
        # Arrange
        decoys = self.plant_decoys()

        # Act
        run = self.run_script([str(self.scripts_dir / START_SCRIPT), SESSION_ID])

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "start-session")
        self.assertEqual(self.decoy_calls(), [], f"a decoy CLI was invoked: {decoys}")

    def test_takes_ids_and_client_from_the_environment_when_they_are_set(self):
        # Arrange
        env = self.script_env(
            LLM_MEMORY_PROJECT_ID="my-project",
            LLM_MEMORY_USER_ID="someone",
            LLM_MEMORY_CLIENT="another-client",
        )

        # Act
        run = self.run_script([str(self.scripts_dir / START_SCRIPT), SESSION_ID], env=env)

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(option_value(calls[0], "--project-id"), "my-project")
        self.assertEqual(option_value(calls[0], "--user-id"), "someone")
        self.assertEqual(option_value(calls[0], "--client"), "another-client")

    def test_accepts_a_trailing_slash_and_spaces_in_memory_mcp_path(self):
        spaced = self.make_cli_tree("memory mcp with spaces")
        for label, value, tree in (
            ("trailing slash", f"{self.cli_tree}/", self.cli_tree),
            ("spaces", str(spaced), spaced),
        ):
            with self.subTest(memory_mcp_path=label):
                # Arrange
                self.call_log.write_text("", encoding="utf-8")

                # Act
                run = self.run_script(
                    [str(self.scripts_dir / START_SCRIPT), SESSION_ID],
                    env=self.script_env(MEMORY_MCP_PATH=value),
                )

                # Assert
                self.assert_succeeded(run)
                calls = self.cli_calls()
                self.assertEqual(len(calls), 1, calls)
                self.assert_used_cli_under(calls[0], tree, "start-session")

    def test_reports_the_variable_and_calls_nothing_when_memory_mcp_path_is_unusable(self):
        # Arrange
        self.plant_decoys()

        for label, value in self.unusable_memory_mcp_paths():
            with self.subTest(memory_mcp_path=label):
                # Arrange
                self.call_log.write_text("", encoding="utf-8")
                self.decoy_log.write_text("", encoding="utf-8")

                # Act
                run = self.run_script(
                    [str(self.scripts_dir / START_SCRIPT), SESSION_ID],
                    env=self.script_env(MEMORY_MCP_PATH=value),
                )

                # Assert
                self.assert_refused_for_memory_mcp_path(run)


class TestCodexMemoryStop(CodexScriptTestBase):
    def test_ends_the_session_through_the_cli_under_memory_mcp_path(self):
        # Act
        run = self.run_script([str(self.scripts_dir / STOP_SCRIPT), SESSION_ID])

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "end-session")
        self.assertEqual(option_value(calls[0], "--session-id"), SESSION_ID)
        self.assertIn("--append-summary-event", calls[0])
        self.assertIn("--extract", calls[0])
        self.assertIn("--consolidate", calls[0])
        self.assertIsNone(option_value(calls[0], "--summary"))

    def test_passes_a_summary_through_when_one_is_given(self):
        # Arrange
        summary = "まとめ\nwith a newline"

        # Act
        run = self.run_script([str(self.scripts_dir / STOP_SCRIPT), SESSION_ID, summary])

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(option_value(calls[0], "--summary"), summary)

    def test_ignores_cli_trees_beside_the_script_and_under_the_home_directory(self):
        # Arrange
        decoys = self.plant_decoys()

        # Act
        run = self.run_script([str(self.scripts_dir / STOP_SCRIPT), SESSION_ID])

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "end-session")
        self.assertEqual(self.decoy_calls(), [], f"a decoy CLI was invoked: {decoys}")

    def test_reports_usage_and_calls_nothing_without_a_session_id(self):
        # Act
        run = self.run_script([str(self.scripts_dir / STOP_SCRIPT)])

        # Assert
        self.assertEqual(run.returncode, 1)
        self.assertIn("usage:", run.stderr)
        self.assertEqual(self.cli_calls(), [])

    def test_reports_the_variable_and_calls_nothing_when_memory_mcp_path_is_unusable(self):
        # Arrange
        self.plant_decoys()

        for label, value in self.unusable_memory_mcp_paths():
            with self.subTest(memory_mcp_path=label):
                # Arrange
                self.call_log.write_text("", encoding="utf-8")
                self.decoy_log.write_text("", encoding="utf-8")

                # Act
                run = self.run_script(
                    [str(self.scripts_dir / STOP_SCRIPT), SESSION_ID],
                    env=self.script_env(MEMORY_MCP_PATH=value),
                )

                # Assert
                self.assert_refused_for_memory_mcp_path(run)

    def test_checks_the_arguments_before_the_environment_variable(self):
        # Act
        run = self.run_script(
            [str(self.scripts_dir / STOP_SCRIPT)], env=self.script_env(MEMORY_MCP_PATH=None)
        )

        # Assert
        self.assertEqual(run.returncode, 1)
        self.assertIn("usage:", run.stderr)
        self.assertEqual(self.cli_calls(), [])


class TestCodexMemoryLog(CodexScriptTestBase):
    def test_appends_the_event_through_the_cli_under_memory_mcp_path(self):
        # Act
        run = self.run_script(
            [str(self.scripts_dir / LOG_SCRIPT), SESSION_ID, "user", "message", "本文", "0.8"]
        )

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "append-event")
        self.assertEqual(option_value(calls[0], "--session-id"), SESSION_ID)
        self.assertEqual(option_value(calls[0], "--role"), "user")
        self.assertEqual(option_value(calls[0], "--kind"), "message")
        self.assertEqual(option_value(calls[0], "--content"), "本文")
        self.assertEqual(option_value(calls[0], "--importance"), "0.8")

    def test_uses_empty_content_and_default_importance_when_they_are_omitted(self):
        # Act
        run = self.run_script(
            [str(self.scripts_dir / LOG_SCRIPT), SESSION_ID, "assistant", "message"]
        )

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(option_value(calls[0], "--content"), "")
        self.assertEqual(option_value(calls[0], "--importance"), "0.5")

    def test_ignores_cli_trees_beside_the_script_and_under_the_home_directory(self):
        # Arrange
        decoys = self.plant_decoys()

        # Act
        run = self.run_script(
            [str(self.scripts_dir / LOG_SCRIPT), SESSION_ID, "user", "message", "本文"]
        )

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "append-event")
        self.assertEqual(self.decoy_calls(), [], f"a decoy CLI was invoked: {decoys}")

    def test_reports_usage_and_calls_nothing_with_too_few_arguments(self):
        # Act
        run = self.run_script([str(self.scripts_dir / LOG_SCRIPT), SESSION_ID, "user"])

        # Assert
        self.assertEqual(run.returncode, 1)
        self.assertIn("usage:", run.stderr)
        self.assertEqual(self.cli_calls(), [])

    def test_reports_the_variable_and_calls_nothing_when_memory_mcp_path_is_unusable(self):
        # Arrange
        self.plant_decoys()

        for label, value in self.unusable_memory_mcp_paths():
            with self.subTest(memory_mcp_path=label):
                # Arrange
                self.call_log.write_text("", encoding="utf-8")
                self.decoy_log.write_text("", encoding="utf-8")

                # Act
                run = self.run_script(
                    [str(self.scripts_dir / LOG_SCRIPT), SESSION_ID, "user", "message", "本文"],
                    env=self.script_env(MEMORY_MCP_PATH=value),
                )

                # Assert
                self.assert_refused_for_memory_mcp_path(run)

    def test_checks_the_arguments_before_the_environment_variable(self):
        # Act
        run = self.run_script(
            [str(self.scripts_dir / LOG_SCRIPT), SESSION_ID, "user"],
            env=self.script_env(MEMORY_MCP_PATH=None),
        )

        # Assert
        self.assertEqual(run.returncode, 1)
        self.assertIn("usage:", run.stderr)
        self.assertEqual(self.cli_calls(), [])


class TestCodexMemoryRun(CodexScriptTestBase):
    def run_wrapper(self, *arguments: str, **kwargs) -> ScriptRun:
        env = kwargs.pop("env", None) or self.script_env(LLM_MEMORY_SESSION_ID=SESSION_ID)
        script = kwargs.pop("script", self.scripts_dir / RUN_SCRIPT)
        return self.run_script([str(script), *arguments], env=env, **kwargs)

    def test_brackets_codex_with_the_start_and_stop_scripts_next_to_it(self):
        # Act
        run = self.run_wrapper("--model", "gpt-5")

        # Assert
        self.assert_succeeded(run)
        self.assertIn(CODEX_OUTPUT, run.stdout)
        self.assertEqual(self.codex_calls(), [["--model", "gpt-5"]])
        calls = self.cli_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "start-session")
        self.assertEqual(option_value(calls[0], "--session-id"), SESSION_ID)
        self.assert_used_cli_under(calls[1], self.cli_tree, "end-session")
        self.assertEqual(option_value(calls[1], "--session-id"), SESSION_ID)
        self.assertEqual(option_value(calls[1], "--summary"), CODEX_OUTPUT)

    def test_ignores_wrappers_and_cli_trees_outside_its_own_directory(self):
        # Arrange
        decoys = self.plant_decoys()

        # Act
        run = self.run_wrapper()

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "start-session")
        self.assert_used_cli_under(calls[1], self.cli_tree, "end-session")
        self.assertEqual(self.decoy_calls(), [], f"a decoy was invoked: {decoys}")

    def test_resolves_its_neighbours_from_a_copy_in_another_directory(self):
        # Arrange
        relocated = self.root / "elsewhere" / "deeply" / "nested"
        relocated.parent.mkdir(parents=True)
        shutil.copytree(self.scripts_dir, relocated)

        # Act
        run = self.run_wrapper(script=relocated / RUN_SCRIPT)

        # Assert
        self.assert_succeeded(run)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "start-session")
        self.assert_used_cli_under(calls[1], self.cli_tree, "end-session")

    def test_ends_the_session_and_reports_when_codex_is_missing(self):
        # Arrange
        env = self.script_env(LLM_MEMORY_SESSION_ID=SESSION_ID, PATH=MINIMAL_PATH)

        # Act
        run = self.run_wrapper(env=env)

        # Assert
        self.assertEqual(run.returncode, 127)
        self.assertIn("codex command not found", run.stderr)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assert_used_cli_under(calls[0], self.cli_tree, "start-session")
        self.assert_used_cli_under(calls[1], self.cli_tree, "end-session")

    def test_reports_the_exit_code_of_codex(self):
        # Arrange
        self.write_executable(
            self.codex_stub,
            """\
            printf 'boom\\n'
            exit 42
            """,
        )

        # Act
        run = self.run_wrapper()

        # Assert
        self.assertEqual(run.returncode, 42)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assert_used_cli_under(calls[1], self.cli_tree, "end-session")
        self.assertEqual(option_value(calls[1], "--summary"), "boom")

    def test_reports_the_variable_once_and_never_starts_codex_when_it_is_unusable(self):
        # Arrange
        self.plant_decoys()

        for label, value in self.unusable_memory_mcp_paths():
            with self.subTest(memory_mcp_path=label):
                # Arrange
                self.call_log.write_text("", encoding="utf-8")
                self.decoy_log.write_text("", encoding="utf-8")
                self.codex_log.write_text("", encoding="utf-8")

                # Act: run_script が TMPDIR に残骸が無いことも確かめる。
                run = self.run_wrapper(
                    env=self.script_env(
                        LLM_MEMORY_SESSION_ID=SESSION_ID, MEMORY_MCP_PATH=value
                    )
                )

                # Assert
                self.assert_refused_for_memory_mcp_path(run)
                self.assertEqual(self.codex_calls(), [], "codex must not be started")
                self.assertEqual(
                    run.stderr.count("MEMORY_MCP_PATH"),
                    1,
                    f"the error must be reported once: {run.stderr!r}",
                )

    def test_checks_the_environment_variable_before_creating_a_temporary_file(self):
        # Arrange: 一時ファイルを作れない TMPDIR。環境変数の検査が mktemp より
        # 後ろにあると、報告されるのは MEMORY_MCP_PATH ではなく mktemp の失敗に
        # なる。run.sh 自身の検査が欠けている場合も同じ形で露見する: start.sh の
        # 検査は mktemp より後ろで走るため、ここでは代わりを務められない。
        self.plant_decoys()
        unusable_tmpdir = self.root / "missing-tmpdir"

        for label, value in self.unusable_memory_mcp_paths():
            with self.subTest(memory_mcp_path=label):
                # Arrange
                self.call_log.write_text("", encoding="utf-8")
                self.decoy_log.write_text("", encoding="utf-8")
                self.codex_log.write_text("", encoding="utf-8")

                # Act
                run = self.run_wrapper(
                    env=self.script_env(
                        LLM_MEMORY_SESSION_ID=SESSION_ID,
                        MEMORY_MCP_PATH=value,
                        TMPDIR=str(unusable_tmpdir),
                    )
                )

                # Assert
                self.assert_refused_for_memory_mcp_path(run)
                self.assertEqual(self.codex_calls(), [], "codex must not be started")
                self.assertEqual(
                    run.stderr.count("MEMORY_MCP_PATH"),
                    1,
                    f"the error must be reported once: {run.stderr!r}",
                )


if __name__ == "__main__":
    unittest.main()
