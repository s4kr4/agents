#!/usr/bin/env python3
"""Tests for hook-session-start-philosophy.sh (SessionStart philosophy injection).

Every run uses a copy of memory/ inside a temporary tree and an environment
built from scratch (no inherited variables, TMPDIR pointed into the tree), so
neither the real Vault nor the repository's own memory/vault and memory/local
can be written to.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

SOURCE_MEMORY_DIR = Path(__file__).resolve().parent
SOURCE_REPO_ROOT = SOURCE_MEMORY_DIR.parent
REAL_DATA_DIRS = (SOURCE_MEMORY_DIR / "vault", SOURCE_MEMORY_DIR / "local")
HOOK_NAME = "hook-session-start-philosophy.sh"
COPIED_FILE_NAMES = {"pyproject.toml", "uv.lock"}
MINIMAL_PATH = "/usr/bin:/bin"
TIMEOUT_COMMAND_NAMES = ("timeout", "gtimeout")

HEADING = "## ユーザーの作業方針（共有メモリ philosophy、自動注入）"
INSTRUCTION = (
    "設計判断ではこの方針に照らして判断し、該当する項目を根拠として示すこと。"
    "プロジェクト固有の規約（AGENTS.md / CLAUDE.md 等）と衝突する場合はプロジェクト規約を優先する。"
)
NOTICE = (
    "共有メモリから作業方針（philosophy）を自動で読み込めませんでした。"
    '設計判断の前に shared-memory の search（scope=global, tags=["philosophy"]）で取得してください。'
)
MAX_ITEM_CHARS = 300
MAX_BODY_CHARS = 2000
ELLIPSIS = "…"
RUN_DEADLINE_SECONDS = 25.0
# For runs that should finish quickly: a hook that hangs gives up sooner.
SHORT_RUN_DEADLINE_SECONDS = 10.0
# Delays for the one-shared-limit test. Together they always exceed a one-second limit
# (even if the hook runs jq only once); apart, neither the CLI (0.8s) nor the jq calls
# (0.4s each) reach it, so splitting the limit in two would let the run finish.
CLI_DELAY_SECONDS = 0.8
JQ_DELAY_SECONDS = 0.4
LARGE_RESPONSE_COUNT = 3000
LARGE_RESPONSE_MAX_ELAPSED = 3.0
SLEEPING_STUB_SECONDS = 30
# The default (5s) must be distinguishable from the largest valid value (8s).
DEFAULT_TIMEOUT_MIN_ELAPSED = 4.5
DEFAULT_TIMEOUT_MAX_ELAPSED = 7.0

LOCALE_VARIANTS: tuple[tuple[str, dict[str, str]], ...] = (
    ("no locale variables", {}),
    ("LC_ALL=C.UTF-8", {"LC_ALL": "C.UTF-8"}),
    ("LC_ALL=en_US.UTF-8", {"LC_ALL": "en_US.UTF-8"}),
    ("LC_ALL=ja_JP.UTF-8", {"LC_ALL": "ja_JP.UTF-8"}),
)

# (summary as injected, memory id)
Entry = tuple[str, str]


def omission_line(count: int) -> str:
    return f"（ほか {count} 件は shared-memory search で取得）"


def item_line(summary: str, memory_id: str) -> str:
    return f"- {summary} [{memory_id}]"


def expected_context(entries: list[Entry], omitted: int = 0) -> str:
    lines = [HEADING, INSTRUCTION, ""] + [item_line(summary, mid) for summary, mid in entries]
    if omitted:
        lines.append(omission_line(omitted))
    return "\n".join(lines)


def expected_within_budget(entries: list[Entry]) -> str:
    """Longest id-ordered prefix whose context (omission line included) fits the budget."""
    total = len(entries)
    for adopted in range(total, -1, -1):
        context = expected_context(entries[:adopted], total - adopted)
        if len(context) <= MAX_BODY_CHARS:
            return context
    raise AssertionError("even the heading alone does not fit the budget")


def numbered_summary(index: int, length: int) -> str:
    label = f"項目{index:02d}"
    return label + "字" * (length - len(label))


def _run_probe(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, env=env, cwd="/", capture_output=True, text=True, timeout=60, check=False
    )


def _python_can_import_yaml() -> bool:
    try:
        result = _run_probe([sys.executable, "-c", "import yaml"], {"PATH": MINIMAL_PATH})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _working_timeout_command() -> str | None:
    """A `timeout` from the minimal PATH that actually stops a command (probed, not just found)."""
    path = shutil.which("timeout", path=MINIMAL_PATH)
    if path is None:
        return None
    try:
        result = _run_probe([path, "1", "sleep", "5"], {"PATH": MINIMAL_PATH})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if result.returncode == 124 else None


YAML_AVAILABLE = _python_can_import_yaml()
JQ_PATH = shutil.which("jq")
PS_PATH = shutil.which("ps", path=MINIMAL_PATH)
REAL_TIMEOUT = _working_timeout_command()


def locale_is_usable(extra_env: dict[str, str]) -> bool:
    """Probe the locale by behaviour: a UTF-8 locale counts "あい" as 2 characters."""
    if not extra_env:
        return True
    result = _run_probe(
        ["/bin/bash", "-c", 'x="あい"; printf "%s" "${#x}"'], {"PATH": MINIMAL_PATH, **extra_env}
    )
    return result.stdout == "2" and result.stderr == ""


def abort_if_unsafe_temp_root(root: Path) -> None:
    """Stop the whole run, not just one test, if the isolated tree could overlap real data."""
    root = root.resolve()
    temp_base = Path(tempfile.gettempdir()).resolve()
    problems = []
    if root == temp_base or not root.is_relative_to(temp_base):
        problems.append(f"{root} is not inside {temp_base}")
    if root.is_relative_to(SOURCE_REPO_ROOT) or SOURCE_REPO_ROOT.is_relative_to(root):
        problems.append(f"{root} overlaps the repository {SOURCE_REPO_ROOT}")
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


@contextmanager
def assert_real_data_unchanged(test: unittest.TestCase) -> Iterator[None]:
    before = {path: snapshot_tree(path) for path in REAL_DATA_DIRS}
    yield
    for path in REAL_DATA_DIRS:
        test.assertTrue(snapshot_tree(path) == before[path], f"{path} changed during the run")


def copy_memory_sources(dest_memory_dir: Path) -> None:
    """Copy the scripts and modules of memory/ (never vault/, local/ or other data)."""
    dest_memory_dir.mkdir(parents=True)
    for source in sorted(SOURCE_MEMORY_DIR.iterdir()):
        if not source.is_file() or source.name.startswith("test_"):
            continue
        if source.suffix in {".py", ".sh"} or source.name in COPIED_FILE_NAMES:
            shutil.copy2(source, dest_memory_dir / source.name)


def shell_code_lines(script: str) -> list[tuple[int, str]]:
    """Lines of a shell script with whole-line comments and the shebang removed."""
    return [
        (number, line)
        for number, line in enumerate(script.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


# Constructs that bash 3.2 (the macOS system bash) does not understand.
BASH4_ONLY_CONSTRUCTS: tuple[tuple[str, str], ...] = (
    ("associative array", r"\b(declare|local|typeset|readonly)\s+-[A-Za-z]*A"),
    ("nameref", r"\b(declare|local|typeset)\s+-[A-Za-z]*n\b"),
    ("declare -g", r"\bdeclare\s+-[A-Za-z]*g"),
    ("local -", r"\blocal\s+-(\s|;|$)"),
    ("mapfile / readarray", r"\b(mapfile|readarray)\b"),
    ("case modification", r"\$\{[#!]?[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?(\^|,)"),
    ("parameter transformation", r"\$\{[#!]?[A-Za-z_@*][A-Za-z0-9_]*(\[[^]]*\])?@[QEPAaKkUuL]\}"),
    ("&>> redirection", r"&>>"),
    ("|& pipe", r"\|&"),
    ("case fall-through", r";;?&"),
    ("coproc", r"\bcoproc\b"),
    ("negative array subscript", r"\$\{[A-Za-z_][A-Za-z0-9_]*\[\s*-"),
    ("negative substring length", r"\$\{[A-Za-z_][A-Za-z0-9_]*:[^:}]*:\s*-"),
    ("wait -n", r"\bwait\s+-[A-Za-z]*n"),
    ("-v variable test", r"(\[\[|\[|\btest)\s+-v\s"),
    ("automatic file descriptor", r"\{[A-Za-z_][A-Za-z0-9_]*\}[<>]"),
    ("printf %(...)T", r"%\([^)]*\)T"),
    ("globstar", r"\bglobstar\b"),
    ("bash 4+ variable", r"\b(BASHPID|EPOCHSECONDS|EPOCHREALTIME|BASH_ARGV0)\b"),
    ("brace expansion increment", r"\{[^{}\s]+\.\.[^{}\s]+\.\.[^{}\s]+\}"),
    ("read -i / -N", r"\bread\s+(-[A-Za-z]+\s+)*-[A-Za-z]*[iN]"),
)


@dataclass
class HookRun:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


class TestHookScriptFile(unittest.TestCase):
    def test_hook_is_an_executable_bash_script_in_memory_directory(self):
        hook = SOURCE_MEMORY_DIR / HOOK_NAME

        self.assertTrue(hook.is_file(), f"missing hook script: {hook}")
        self.assertTrue(os.access(hook, os.X_OK), "hook script must be executable")
        first_line = hook.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first_line.startswith("#!"), first_line)
        self.assertIn("bash", first_line)

    def test_hook_uses_no_constructs_unavailable_in_bash_3_2(self):
        # Arrange
        lines = shell_code_lines((SOURCE_MEMORY_DIR / HOOK_NAME).read_text(encoding="utf-8"))
        self.assertTrue(lines, "hook script has no code lines")

        # Act
        findings = [
            f"line {number} ({label}): {line.strip()}"
            for number, line in lines
            for label, pattern in BASH4_ONLY_CONSTRUCTS
            if re.search(pattern, line)
        ]

        # Assert
        self.assertEqual(findings, [])


class HookTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hook-philosophy-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        abort_if_unsafe_temp_root(self.root)
        self.memory_dir = self.root / "repo" / "memory"
        copy_memory_sources(self.memory_dir)
        self.hook = self.memory_dir / HOOK_NAME
        self.vault = self.root / "vault"
        self.local_dir = self.root / "local"
        self.queue_dir = self.root / "queue"
        self.home = self.root / "home"
        self.tmpdir = self.root / "tmp"
        self.config = self.root / "config" / "empty-config.toml"
        self.jq_bin = self.root / "bin-with-jq"
        self.workdir = self.root / "work"
        for directory in (self.home, self.tmpdir, self.config.parent, self.jq_bin, self.workdir):
            directory.mkdir(parents=True)
        self.config.write_text("", encoding="utf-8")
        if JQ_PATH:
            (self.jq_bin / "jq").symlink_to(JQ_PATH)
        self.hook_sessions: list[int] = []
        self.addCleanup(self.kill_hook_session_processes)

    # -- environment -------------------------------------------------------

    def hook_env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "TMPDIR": str(self.tmpdir),
            "PATH": f"{self.jq_bin}:{MINIMAL_PATH}",
            "LLM_MEMORY_VAULT": str(self.vault),
            "LLM_MEMORY_LOCAL_DIR": str(self.local_dir),
            "LLM_MEMORY_QUEUE_DIR": str(self.queue_dir),
            "LLM_MEMORY_CONFIG": str(self.config),
            "LLM_MEMORY_PYTHON": sys.executable,
        }
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    @contextmanager
    def isolated_process_environment(self) -> Iterator[None]:
        # The store API resolves its lock directory from XDG_CACHE_HOME/HOME.
        with patch.dict(os.environ, self.hook_env(), clear=True):
            yield

    # -- fixtures ----------------------------------------------------------

    def seed(
        self,
        key: str,
        summary: str,
        *,
        tags: tuple[str, ...] = ("philosophy",),
        scope: str = "global",
        project_id: str | None = None,
        updated: str | None = None,
        title: str | None = None,
    ) -> str:
        from markdown_store import MarkdownMemoryStore

        with self.isolated_process_environment():
            store = MarkdownMemoryStore(self.vault)
            record = store.upsert_from_observation(
                type="feedback",
                entity_type="user",
                entity_id="default",
                key=key,
                scope=scope,
                project_id=project_id,
                summary=summary,
                tags=list(tags),
            )
            if updated is not None or title is not None:
                if updated is not None:
                    record["updated"] = updated
                if title is not None:
                    record["title"] = title
                store.write(record)
            stored = store.read(record["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["summary"], summary, "fixture summary was not stored verbatim")
        if title is not None:
            self.assertEqual(stored["title"], title, "fixture title was not stored verbatim")
        return record["id"]

    def forget(self, memory_id: str) -> None:
        from markdown_store import MarkdownMemoryStore

        with self.isolated_process_environment():
            self.assertEqual(MarkdownMemoryStore(self.vault).forget(memory_id), 1)

    def store_search_order(self) -> list[str]:
        from markdown_store import MarkdownMemoryStore

        with self.isolated_process_environment():
            return [
                record["id"]
                for record in MarkdownMemoryStore(self.vault).search(
                    scope="global", tags=["philosophy"]
                )
            ]

    def make_stub(self, name: str, body: str) -> Path:
        path = self.root / "stubs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/bash\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def make_sleeping_stub(self) -> Path:
        # The sleep stays a child process (not exec'd) because `uv run` also keeps
        # python as a child; the hook's time limit must stop the whole process tree.
        return self.make_stub(
            "sleeping-python",
            f"""\
            sleep {SLEEPING_STUB_SECONDS} &
            wait
            """,
        )

    def make_response_stub(self, name: str, response: Any) -> Path:
        """A CLI stand-in that prints ``response`` as JSON and exits 0."""
        response_file = self.root / "stubs" / f"{name}.json"
        response_file.parent.mkdir(exist_ok=True)
        response_file.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return self.make_stub(name, f"cat {shlex.quote(str(response_file))}\n")

    def make_delayed_jq_bin(self, delay: float, name: str = "bin-with-delayed-jq") -> Path:
        """A PATH directory whose jq waits ``delay`` seconds before reading any input.

        Calls that only ask about jq itself (--version, --help, -n) answer at once, so a
        hook can still check that jq exists without paying the delay.
        """
        assert JQ_PATH is not None
        bin_dir = self.root / name
        bin_dir.mkdir()
        delayed_jq = bin_dir / "jq"
        delayed_jq.write_text(
            "#!/bin/bash\n"
            'for argument in "$@"; do\n'
            '  case "$argument" in\n'
            "    --version|--help|-h|--null-input|-n*|-[!-]*n*)\n"
            f'      exec {shlex.quote(JQ_PATH)} "$@" ;;\n'
            "  esac\n"
            "done\n"
            f"sleep {delay}\n"
            f'exec {shlex.quote(JQ_PATH)} "$@"\n',
            encoding="utf-8",
        )
        delayed_jq.chmod(0o755)
        for probe_arguments in (["-n", "1"], ["-cn", "1"], ["--version"]):
            probe_started = time.monotonic()
            probe = _run_probe([str(delayed_jq), *probe_arguments], {"PATH": MINIMAL_PATH})
            self.assertEqual(probe.returncode, 0, probe_arguments)
            self.assertLess(time.monotonic() - probe_started, 5.0, probe_arguments)
        return bin_dir

    # -- processes ---------------------------------------------------------

    def live_processes_in_session(self, session_id: int) -> list[int]:
        """Non-zombie processes still in the session the test created for one hook run."""
        if PS_PATH is None:
            return []
        listing = subprocess.run(
            [PS_PATH, "-A", "-o", "pid=", "-o", "stat="],
            env={"PATH": MINIMAL_PATH},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        pids = []
        for line in listing.stdout.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[1].startswith("Z"):
                continue
            pid = int(fields[0])
            with suppress(ProcessLookupError, PermissionError):
                if os.getsid(pid) == session_id:
                    pids.append(pid)
        return pids

    def wait_for_session_to_empty(self, session_id: int, seconds: float = 3.0) -> list[int]:
        deadline = time.monotonic() + seconds
        while True:
            remaining = self.live_processes_in_session(session_id)
            if not remaining or time.monotonic() >= deadline:
                return remaining
            time.sleep(0.1)

    def kill_hook_session_processes(self) -> None:
        for session_id in self.hook_sessions:
            for pid in self.live_processes_in_session(session_id):
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)

    # -- running -----------------------------------------------------------

    def run_hook(
        self,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        deadline: float = RUN_DEADLINE_SECONDS,
    ) -> HookRun:
        if not self.hook.is_file():
            self.fail(f"hook script is missing: {self.hook}")
        if env is None:
            env = self.hook_env()
        if stdin is None:
            stdin = json.dumps(
                {
                    "session_id": "test-session",
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "cwd": str(self.workdir),
                }
            )
        tmpdir_before = sorted(os.listdir(self.tmpdir))
        started = time.monotonic()
        process = subprocess.Popen(
            [str(self.hook)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self.workdir,
            start_new_session=True,
        )
        self.hook_sessions.append(process.pid)
        try:
            stdout, stderr = process.communicate(stdin.encode("utf-8"), timeout=deadline)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            self.fail(f"hook did not finish (or left its output open) within {deadline} seconds")
        elapsed = time.monotonic() - started
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)), tmpdir_before, "hook left temporary files in TMPDIR"
        )
        try:
            stdout_text = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.fail(f"stdout is not UTF-8 ({exc}): {stdout!r}")
        return HookRun(process.returncode, stdout_text, stderr.decode("utf-8", "replace"), elapsed)

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        self.kill_hook_session_processes()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    # -- assertions --------------------------------------------------------

    def assert_injected(self, run: HookRun, context: str) -> None:
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stderr, "", "hook must not write to stderr")
        try:
            payload = json.loads(run.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"stdout must be exactly one JSON document ({exc}): {run.stdout!r}")
        self.assertIsInstance(payload, dict)
        self.assertEqual(list(payload), ["hookSpecificOutput"])
        output = payload["hookSpecificOutput"]
        self.assertIsInstance(output, dict)
        self.assertEqual(sorted(output), ["additionalContext", "hookEventName"])
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertEqual(output["additionalContext"], context)

    def assert_notice(self, run: HookRun) -> None:
        self.assert_injected(run, NOTICE)

    def assert_no_output(self, run: HookRun) -> None:
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stdout, "")
        self.assertEqual(run.stderr, "")


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookInjectsPhilosophy(HookTestBase):
    def test_injects_heading_instruction_and_each_summary_with_id_as_single_json(self):
        # Arrange
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        self.seed("philosophy-minimal-change", "変更は最小限にする")

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(
            run,
            expected_context(
                [
                    ("変更は最小限にする", "global/philosophy-minimal-change"),
                    ("一つのことをうまくやる", "global/philosophy-one-thing"),
                ]
            ),
        )

    def test_drops_key_prefix_from_summaries_written_by_write_memory(self):
        # Arrange
        for key, summary in (
            ("philosophy-minimal-change", "変更は最小限にする"),
            ("philosophy_small_steps", "小さく進める"),
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.memory_dir / "memory.py"),
                    "write-memory",
                    "--session-id",
                    "seed-session",
                    "--memory-type",
                    "feedback",
                    "--key",
                    key,
                    "--summary",
                    summary,
                    "--scope",
                    "global",
                    "--tag",
                    "philosophy",
                ],
                env=self.hook_env(),
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        from markdown_store import MarkdownMemoryStore

        with self.isolated_process_environment():
            store = MarkdownMemoryStore(self.vault)
            stored = {
                memory_id: store.read(memory_id)["summary"]
                for memory_id in (
                    "global/philosophy-minimal-change",
                    "global/philosophy-small-steps",
                )
            }
        self.assertEqual(
            stored,
            {
                "global/philosophy-minimal-change": "philosophy-minimal-change: 変更は最小限にする",
                "global/philosophy-small-steps": "philosophy_small_steps: 小さく進める",
            },
        )

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(
            run,
            expected_context(
                [
                    ("変更は最小限にする", "global/philosophy-minimal-change"),
                    ("小さく進める", "global/philosophy-small-steps"),
                ]
            ),
        )

    def test_includes_only_active_global_memories_tagged_philosophy(self):
        # Arrange
        self.seed("philosophy-kept", "対象になる")
        self.seed("philosophy-multi-tag", "複数タグでも対象", tags=("philosophy", "design"))
        self.seed("untagged-global", "philosophy という語を含むがタグなし", tags=())
        self.seed("other-tag-global", "別タグだけの記憶", tags=("design",))
        self.seed(
            "philosophy-project", "プロジェクトの記憶", scope="project", project_id="sample-project"
        )
        forgotten = self.seed("philosophy-forgotten", "忘れた記憶")
        self.forget(forgotten)

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(
            run,
            expected_context(
                [
                    ("対象になる", "global/philosophy-kept"),
                    ("複数タグでも対象", "global/philosophy-multi-tag"),
                ]
            ),
        )

    def test_orders_items_by_id_code_points_not_update_time_or_locale_collation(self):
        # Arrange
        self.seed("philosophy-aa", "4番目", updated="2026-09-04T00:00:00+09:00")
        self.seed("philosophy-a2", "3番目", updated="2026-09-01T00:00:00+09:00")
        self.seed("philosophy-a-z", "1番目", updated="2026-09-02T00:00:00+09:00")
        self.seed("philosophy-a10", "2番目", updated="2026-09-03T00:00:00+09:00")
        default_order = self.store_search_order()
        self.assertNotEqual(default_order, sorted(default_order), "fixture must not be id-ordered")
        expected = expected_context(
            [
                ("1番目", "global/philosophy-a-z"),
                ("2番目", "global/philosophy-a10"),
                ("3番目", "global/philosophy-a2"),
                ("4番目", "global/philosophy-aa"),
            ]
        )

        for label, extra_env in LOCALE_VARIANTS:
            with self.subTest(locale=label):
                if not locale_is_usable(extra_env):
                    self.skipTest(f"{label} is not available on this machine")

                # Act
                run = self.run_hook(self.hook_env(**extra_env))

                # Assert
                self.assert_injected(run, expected)

    def test_emits_nothing_when_no_philosophy_memory_exists(self):
        with self.subTest(vault="empty"):
            self.assert_no_output(self.run_hook())

        with self.subTest(vault="only non-matching memories"):
            # Arrange
            self.seed("untagged-global", "タグなし", tags=())
            self.seed(
                "philosophy-project", "プロジェクトの記憶", scope="project", project_id="sample"
            )
            self.forget(self.seed("philosophy-forgotten", "忘れた記憶"))

            # Act / Assert
            self.assert_no_output(self.run_hook())

    def test_output_does_not_depend_on_stdin(self):
        # Arrange
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        expected = expected_context([("一つのことをうまくやる", "global/philosophy-one-thing")])
        payload = {
            "session_id": "abc",
            "hook_event_name": "SessionStart",
            "source": "resume",
            "cwd": "/somewhere/else",
            "transcript_path": "/nonexistent/transcript.jsonl",
        }

        for label, stdin in (
            ("empty", ""),
            ("invalid json", '{"source": "startup",'),
            ("session start payload", json.dumps(payload)),
        ):
            with self.subTest(stdin=label):
                # Act
                run = self.run_hook(stdin=stdin)

                # Assert
                self.assert_injected(run, expected)

    def test_runs_memory_cli_next_to_script_through_run_python(self):
        # Arrange
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        argv_file = self.root / "recorded-argv"
        stub = self.make_stub(
            "recording-python",
            f"""\
            printf '%s\\n' "$@" > {shlex.quote(str(argv_file))}
            exec {shlex.quote(sys.executable)} "$@"
            """,
        )

        # Act
        run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)))

        # Assert
        self.assert_injected(
            run, expected_context([("一つのことをうまくやる", "global/philosophy-one-thing")])
        )
        argv = argv_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(os.path.realpath(argv[0]), os.path.realpath(self.memory_dir / "memory.py"))
        self.assertEqual(argv[1:3], ["--require-vault", "search"])
        options = argv[3:]
        adjacent_pairs = set(zip(options, options[1:]))
        self.assertIn(("--scope", "global"), adjacent_pairs)
        self.assertIn(("--tag", "philosophy"), adjacent_pairs)
        search_filters = {
            "--session-id",
            "--query",
            "--entity-id",
            "--memory-type",
            "--scope",
            "--project-id",
            "--tag",
        }
        self.assertEqual(
            sorted(option for option in options if option in search_filters),
            ["--scope", "--tag"],
        )


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookManyMemories(HookTestBase):
    """More philosophy memories than the CLI's default page, newest-updated ids last."""

    def seed_with_younger_ids_updated_earlier(self, count: int) -> list[Entry]:
        from markdown_store import MarkdownMemoryStore

        base = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))
        records = [
            (f"p{index:03d}", f"方針{index:03d}", base + timedelta(minutes=index))
            for index in range(1, count + 1)
        ]
        ids = []
        with self.isolated_process_environment():
            store = MarkdownMemoryStore(self.vault)
            # Every store write regenerates _index.md by re-reading the whole Vault,
            # which makes seeding quadratic. The index is a derived listing that
            # search never reads, so it is rebuilt once by the final write instead.
            with patch.object(MarkdownMemoryStore, "_write_index", lambda _store: None):
                for key, summary, updated in records:
                    record = store.upsert_from_observation(
                        type="feedback",
                        entity_type="user",
                        entity_id="default",
                        key=key,
                        scope="global",
                        project_id=None,
                        summary=summary,
                        tags=["philosophy"],
                    )
                    record["updated"] = updated.isoformat()
                    store.write(record)
                    ids.append(record["id"])
            store.write(record)
        self.assertEqual(ids, [f"global/p{index:03d}" for index in range(1, count + 1)])
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(
            self.store_search_order(),
            list(reversed(ids)),
            "fixture must list the youngest id last when ordered by update time",
        )
        return [(summary, memory_id) for (_, summary, _), memory_id in zip(records, ids)]

    def test_includes_every_memory_beyond_fifty_when_all_fit(self):
        # Arrange
        entries = self.seed_with_younger_ids_updated_earlier(60)
        expected = expected_context(entries)
        self.assertLessEqual(len(expected), MAX_BODY_CHARS)

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(run, expected)

    def test_selects_from_all_memories_in_id_order_and_counts_the_rest_as_omitted(self):
        # Arrange
        entries = self.seed_with_younger_ids_updated_earlier(120)
        expected = expected_within_budget(entries)
        adopted = expected.count("\n- ")
        self.assertGreater(adopted, 50, "fixture must adopt more than one CLI page")
        self.assertLess(adopted, len(entries), "fixture must omit some memories")
        self.assertTrue(expected.endswith(omission_line(len(entries) - adopted)))

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(run, expected)


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookKeyPrefix(HookTestBase):
    def test_removes_one_leading_key_prefix_in_hyphen_or_underscore_form(self):
        # Arrange
        self.seed("philosophy-p01", "philosophy-p01: ハイフン形式の接頭辞")
        self.seed("philosophy-p02", "philosophy_p02: アンダースコア形式の接頭辞")
        self.seed("philosophy-p03", "philosophy-p03: philosophy-p03: 先頭の一つだけ除く")

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(
            run,
            expected_context(
                [
                    ("ハイフン形式の接頭辞", "global/philosophy-p01"),
                    ("アンダースコア形式の接頭辞", "global/philosophy-p02"),
                    ("philosophy-p03: 先頭の一つだけ除く", "global/philosophy-p03"),
                ]
            ),
        )

    def test_keeps_summary_that_does_not_start_with_its_own_key_prefix(self):
        # Arrange
        summaries = [
            ("philosophy-p04", "接頭辞のない本文"),
            ("philosophy-p05", "注意: キーではない語とコロン"),
            ("philosophy-p06", "philosophy-p04: 別の記憶のキー"),
            ("philosophy-p07", "本文の途中の philosophy-p07: は残す"),
            ("philosophy-p08", "philosophy-p08:空白なしは接頭辞ではない"),
            ("philosophy-p09", "philosophy: キーの一部だけ"),
        ]
        for key, summary in summaries:
            self.seed(key, summary)

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(
            run, expected_context([(summary, f"global/{key}") for key, summary in summaries])
        )


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookLengthLimits(HookTestBase):
    @staticmethod
    def numbered_id(index: int) -> str:
        return f"global/philosophy-len-{index:02d}"

    def seed_numbered(self, entries: list[Entry]) -> None:
        for index, (summary, memory_id) in enumerate(entries, start=1):
            self.assertEqual(memory_id, self.numbered_id(index))
            self.assertEqual(self.seed(f"philosophy-len-{index:02d}", summary), memory_id)

    def numbered_entries(self, lengths: list[int]) -> list[Entry]:
        return [
            (numbered_summary(index, length), self.numbered_id(index))
            for index, length in enumerate(lengths, start=1)
        ]

    def entries_totalling(self, total: int) -> list[Entry]:
        """Entries (summaries <= 300 chars) whose full context, ids included, is ``total`` chars."""
        min_last = len(omission_line(1)) + 10
        for count in range(1, 12):
            head = self.numbered_entries([MAX_ITEM_CHARS] * (count - 1))
            last_length = total - len(expected_context([*head, ("", self.numbered_id(count))]))
            if min_last <= last_length <= MAX_ITEM_CHARS:
                entries = [*head, (numbered_summary(count, last_length), self.numbered_id(count))]
                self.assertEqual(len(expected_context(entries)), total)
                return entries
        self.fail(f"cannot build a fixture totalling {total} characters")

    def test_limits_each_summary_to_300_code_points_without_truncating_its_id(self):
        # Arrange
        self.seed("philosophy-l01", "あ" * 300)
        self.seed("philosophy-l02", "い" * 301)
        self.seed("philosophy-l03", "😀" * 301)
        self.seed("philosophy-l04", "philosophy-l04: " + "う" * 300)
        self.seed("philosophy-l05", "philosophy_l05: " + "え" * 301)
        expected = expected_context(
            [
                ("あ" * 300, "global/philosophy-l01"),
                ("い" * 299 + ELLIPSIS, "global/philosophy-l02"),
                ("😀" * 299 + ELLIPSIS, "global/philosophy-l03"),
                ("う" * 300, "global/philosophy-l04"),
                ("え" * 299 + ELLIPSIS, "global/philosophy-l05"),
            ]
        )
        self.assertLessEqual(len(expected), MAX_BODY_CHARS)

        for label, extra_env in LOCALE_VARIANTS:
            with self.subTest(locale=label):
                if not locale_is_usable(extra_env):
                    self.skipTest(f"{label} is not available on this machine")

                # Act
                run = self.run_hook(self.hook_env(**extra_env))

                # Assert
                self.assert_injected(run, expected)

    def test_includes_every_item_when_context_with_ids_is_exactly_2000_characters(self):
        # Arrange
        entries = self.entries_totalling(MAX_BODY_CHARS)
        self.seed_numbered(entries)

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(run, expected_context(entries))

    def test_omits_items_that_do_not_fit_and_appends_omitted_count(self):
        # Arrange
        entries = self.entries_totalling(MAX_BODY_CHARS + 1)
        self.seed_numbered(entries)
        expected = expected_context(entries[:-1], omitted=1)
        self.assertLessEqual(len(expected), MAX_BODY_CHARS)

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(run, expected)

    def test_counts_omission_line_within_the_2000_character_limit(self):
        # Arrange: the first `fitting + 1` items fit only while no omission line is added.
        for length in range(200, MAX_ITEM_CHARS + 1):
            for fitting in range(1, 11):
                entries = self.numbered_entries([length] * (fitting + 3))
                without_line = expected_context(entries[: fitting + 1])
                with_line = expected_context(entries[: fitting + 1], len(entries) - fitting - 1)
                expected = expected_context(entries[:fitting], len(entries) - fitting)
                if (
                    len(without_line) <= MAX_BODY_CHARS < len(with_line)
                    and len(expected) <= MAX_BODY_CHARS
                ):
                    break
            else:
                continue
            break
        else:
            self.fail("cannot build a fixture for the omission-line boundary")
        self.seed_numbered(entries)

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(run, expected)


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookTreatsMemoryContentAsData(HookTestBase):
    def test_preserves_special_characters_without_evaluating_commands(self):
        # Arrange
        markers = {
            name: self.root / f"injected-{name}"
            for name in ("summary-subst", "summary-backtick", "title-subst", "title-backtick")
        }
        first_line = '引用符 " とバックスラッシュ \\ と \\n という文字'
        second_line = (
            f"2 行目 $(touch {markers['summary-subst']}) `touch {markers['summary-backtick']}` "
            "${HOME} %s %d %% \\u0041 タブ"
        )
        summary = f"{first_line}\n{second_line}\tの後"
        title = (
            f'$(touch {markers["title-subst"]}) `touch {markers["title-backtick"]}` "題" \\ 終わり'
        )
        self.seed("philosophy-special", summary, title=title)
        self.seed("philosophy-special-e", "-e \\t\\c")
        self.seed("philosophy-special-n", "-n")

        # Act
        run = self.run_hook()

        # Assert
        self.assert_injected(
            run,
            expected_context(
                [
                    (f"{first_line} {second_line} の後", "global/philosophy-special"),
                    ("-e \\t\\c", "global/philosophy-special-e"),
                    ("-n", "global/philosophy-special-n"),
                ]
            ),
        )
        for name, marker in markers.items():
            self.assertFalse(marker.exists(), f"{name} was evaluated as a command")


def search_response(**changes: Any) -> dict[str, Any]:
    memory: dict[str, Any] = {
        "id": "global/philosophy-stub",
        "type": "feedback",
        "title": "Philosophy Stub",
        "summary": "スタブの本文",
        "scope": "global",
        "project_id": None,
        "entity_id": None,
        "updated": "2026-09-01T00:00:00+09:00",
        "tags": ["philosophy"],
        "related": [],
    }
    response: dict[str, Any] = {"ok": True, "memories": [memory], "count": 1}
    response.update(changes)
    return response


STUB_ENTRY: Entry = ("スタブの本文", "global/philosophy-stub")


def response_memory(memory_id: str, summary: str) -> dict[str, Any]:
    return dict(search_response()["memories"][0], id=memory_id, summary=summary)


def id_ordered(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda entry: entry[1])


@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookBuildsContextFromCliResponse(HookTestBase):
    """Responses too large or too unusual to seed through the store."""

    def run_with_memories(
        self, memories: list[dict[str, Any]], deadline: float = RUN_DEADLINE_SECONDS
    ) -> HookRun:
        # Listed in reverse, so the hook has to sort them by id itself.
        response = search_response(memories=memories[::-1], count=len(memories))
        stub = self.make_response_stub("response-python", response)
        return self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)), deadline=deadline)

    def test_builds_context_within_seconds_from_thousands_of_300_character_memories(self):
        # Arrange
        summary = "字" * MAX_ITEM_CHARS
        entries = id_ordered(
            [(summary, f"global/p{index}") for index in range(1, LARGE_RESPONSE_COUNT + 1)]
        )
        expected = expected_within_budget(entries)
        adopted = expected.count("\n- ")
        self.assertGreater(adopted, 0)
        self.assertTrue(expected.endswith(omission_line(len(entries) - adopted)))

        # Act
        run = self.run_with_memories(
            [response_memory(memory_id, text) for text, memory_id in entries],
            deadline=SHORT_RUN_DEADLINE_SECONDS,
        )

        # Assert
        self.assert_injected(run, expected)
        self.assertLess(run.elapsed, LARGE_RESPONSE_MAX_ELAPSED)

    def test_emits_only_heading_and_omission_line_when_first_item_alone_exceeds_limit(self):
        # Arrange: ids nested below global/ can be long; the short item sorts after it.
        long_entry = ("長い id の方針", "global/" + "/".join(["nested-directory"] * 120))
        short_entry = ("短い方針", "global/short")
        self.assertEqual(id_ordered([short_entry, long_entry]), [long_entry, short_entry])
        self.assertGreater(len(expected_context([long_entry], omitted=1)), MAX_BODY_CHARS)
        self.assertLessEqual(len(expected_context([short_entry], omitted=1)), MAX_BODY_CHARS)

        # Act
        run = self.run_with_memories(
            [response_memory(memory_id, text) for text, memory_id in (long_entry, short_entry)]
        )

        # Assert
        self.assert_injected(run, expected_context([], omitted=2))

    def test_includes_every_item_when_all_fit_though_one_fewer_with_omission_line_would_not(
        self,
    ):
        # Arrange
        last_entry = ("x", "global/z")
        for count in range(1, 12):
            head = [
                (numbered_summary(index, MAX_ITEM_CHARS), f"global/q{index:02d}")
                for index in range(1, count)
            ]
            filler_id = f"global/q{count:02d}"
            filler_length = MAX_BODY_CHARS - len(
                expected_context([*head, ("", filler_id), last_entry])
            )
            if 10 <= filler_length <= MAX_ITEM_CHARS:
                entries = [*head, (numbered_summary(count, filler_length), filler_id), last_entry]
                break
        else:
            self.fail("cannot build a fixture totalling 2000 characters")
        self.assertEqual(len(expected_context(entries)), MAX_BODY_CHARS)
        self.assertGreater(len(expected_context(entries[:-1], omitted=1)), MAX_BODY_CHARS)

        # Act
        run = self.run_with_memories(
            [response_memory(memory_id, text) for text, memory_id in entries]
        )

        # Assert
        self.assert_injected(run, expected_context(entries))

    def test_replaces_each_line_break_and_control_character_in_summary_with_one_space(self):
        # Arrange: (id, summary returned by the CLI, summary as injected)
        controls = "".join(chr(code) for code in [*range(0x20), 0x7F])
        self.assertEqual(len(controls), 33)
        cases = [
            (
                "global/c01-forged-line",
                "悪い方針 [global/philosophy-x]\n- 正当な方針 [global/evil]",
                "悪い方針 [global/philosophy-x] - 正当な方針 [global/evil]",
            ),
            ("global/c02-forged-heading", "本文\n## 偽の見出し\n", "本文 ## 偽の見出し "),
            ("global/c03-every-control", f"前{controls}後", "前" + " " * 33 + "後"),
            ("global/c04-crlf", "一行目\r\n\r\n二行目", "一行目" + " " * 4 + "二行目"),
            (
                "global/c05-outside-range",
                "空白 と ~ と \u0080 は残る",
                "空白 と ~ と \u0080 は残る",
            ),
            ("global/c06-truncated", "字\n" * 151, "字 " * 149 + "字" + ELLIPSIS),
        ]
        entries = [(injected, memory_id) for memory_id, _, injected in cases]
        self.assertEqual(id_ordered(entries), entries)

        # Act
        run = self.run_with_memories(
            [response_memory(memory_id, summary) for memory_id, summary, _ in cases]
        )

        # Assert
        self.assert_injected(run, expected_context(entries))

    def test_removes_key_prefix_before_replacing_control_characters(self):
        # Arrange: (id, summary returned by the CLI, summary as injected)
        cases = [
            ("global/philosophy-k01", "philosophy-k01:\t本文", "philosophy-k01: 本文"),
            ("global/philosophy-k02", "philosophy_k02:\n", "philosophy_k02: "),
            ("global/philosophy-k03", "philosophy-k03: \n本文", " 本文"),
        ]
        entries = [(injected, memory_id) for memory_id, _, injected in cases]

        # Act
        run = self.run_with_memories(
            [response_memory(memory_id, summary) for memory_id, summary, _ in cases]
        )

        # Assert
        self.assert_injected(run, expected_context(entries))

    def test_skips_memories_with_invalid_ids_and_counts_them_as_omitted(self):
        # Arrange
        valid = [
            ("記号を含む id", "global/(valid)-a.b_c"),
            ("入れ子の id", "global/nested/valid"),
            ("正当な方針", "global/philosophy-one-thing"),
            ("非 ASCII の id", "global/方針"),
        ]
        invalid_ids = [
            "global/evil]",
            "global/[evil",
            "global/evil one",
            "global/evil\n## 偽の見出し",
            "global/evil\ttab",
            "global/evil\u0000",
            "global/evil\u001f",
            "global/evil\u007f",
        ]
        self.assertLess(max(invalid_ids), max(memory_id for _, memory_id in valid))
        self.assertGreater(min(invalid_ids), min(memory_id for _, memory_id in valid))
        expected = expected_context(id_ordered(valid), omitted=len(invalid_ids))

        # Act
        run = self.run_with_memories(
            [response_memory(memory_id, text) for text, memory_id in valid]
            + [response_memory(memory_id, "偽装した方針") for memory_id in invalid_ids]
        )

        # Assert
        self.assert_injected(run, expected)

    def test_emits_heading_and_omission_line_when_every_memory_is_skipped(self):
        # Arrange: unlike an empty search result, philosophy memories do exist here, so
        # the omission line tells the session how many to fetch with shared-memory search.
        memories = [
            response_memory("global/evil]", "偽装した方針"),
            response_memory("global/skip-blank", "   "),
            response_memory("global/skip-prefix", "skip-prefix: "),
        ]

        # Act
        run = self.run_with_memories(memories)

        # Assert
        self.assert_injected(run, expected_context([], omitted=len(memories)))

    def test_skips_memories_whose_summary_is_blank_after_prefix_removal_and_counts_them(self):
        # Arrange: (id, summary returned by the CLI, summary as injected or None if skipped)
        cases: list[tuple[str, str, str | None]] = [
            ("global/blank-a", "先頭の方針", "先頭の方針"),
            ("global/blank-b", "", None),
            ("global/blank-c", "blank-c: ", None),
            ("global/blank-d", "   ", None),
            ("global/blank-e", "\n\t\r", None),
            ("global/blank-f", "blank_f: \n", None),
            ("global/blank-g", "blank-g: 残る方針", "残る方針"),
            ("global/blank-h", "blank-h:", "blank-h:"),
            ("global/blank-i", " 前後に空白 ", " 前後に空白 "),
        ]
        kept = [(injected, memory_id) for memory_id, _, injected in cases if injected is not None]

        # Act
        run = self.run_with_memories(
            [response_memory(memory_id, summary) for memory_id, summary, _ in cases]
        )

        # Assert
        self.assert_injected(run, expected_context(kept, omitted=len(cases) - len(kept)))


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookFailureNotice(HookTestBase):
    SEARCH_RESULT = json.dumps(search_response(), ensure_ascii=False)

    def make_output_stub(self, name: str, stdout: str, exit_code: int) -> Path:
        output_file = self.root / "stubs" / f"{name}.stdout"
        output_file.parent.mkdir(exist_ok=True)
        output_file.write_text(stdout, encoding="utf-8")
        return self.make_stub(
            name,
            f"""\
            cat {shlex.quote(str(output_file))}
            printf 'Traceback (most recent call last): secret-detail\\n' >&2
            exit {exit_code}
            """,
        )

    def test_notice_when_vault_cannot_be_resolved(self):
        # Arrange
        variants = (
            ("empty explicit config", self.hook_env(LLM_MEMORY_VAULT=None)),
            ("no config file", self.hook_env(LLM_MEMORY_VAULT=None, LLM_MEMORY_CONFIG=None)),
        )

        for label, env in variants:
            with self.subTest(config=label):
                # Act
                with assert_real_data_unchanged(self):
                    run = self.run_hook(env)

                # Assert
                self.assert_notice(run)
                self.assertFalse((self.memory_dir / "vault").exists())
                self.assertFalse((self.memory_dir / "local").exists())

    def test_notice_when_python_cannot_be_found(self):
        # Arrange
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        env = self.hook_env(LLM_MEMORY_PYTHON="/nonexistent/python3")
        probe = _run_probe(
            [
                "/bin/bash",
                "-c",
                'command -v uv || command -v mise || [ -x "$HOME/.local/bin/mise" ]',
            ],
            env,
        )
        if probe.returncode == 0:
            self.skipTest("uv or mise is reachable from the isolated PATH/HOME")

        # Act
        run = self.run_hook(env)

        # Assert
        self.assert_notice(run)

    def test_notice_without_stderr_when_temporary_directory_is_unusable(self):
        # Arrange
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        read_only = self.root / "read-only-tmp"
        read_only.mkdir()
        read_only.chmod(0o500)
        self.addCleanup(read_only.chmod, 0o700)
        regular_file = self.root / "tmp-is-a-file"
        regular_file.write_text("", encoding="utf-8")
        variants = (
            ("nonexistent", "/nonexistent/dir"),
            ("not writable", str(read_only)),
            ("regular file", str(regular_file)),
        )

        for label, tmpdir in variants:
            with self.subTest(TMPDIR=label):
                if label == "not writable":
                    try:
                        (read_only / "probe").mkdir()
                    except PermissionError:
                        pass
                    else:
                        self.skipTest("directory permissions are not enforced for this user")

                # Act
                with assert_real_data_unchanged(self):
                    run = self.run_hook(self.hook_env(TMPDIR=tmpdir))

                # Assert
                self.assert_notice(run)

    def test_notice_when_cli_exits_nonzero_even_if_stdout_is_valid_json(self):
        with self.subTest(control="same output with exit 0 is injected"):
            stub = self.make_output_stub("ok-python", self.SEARCH_RESULT, 0)
            run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)))
            self.assert_injected(run, expected_context([STUB_ENTRY]))

        with self.subTest(exit_code=1):
            # Arrange
            stub = self.make_output_stub("failing-python", self.SEARCH_RESULT, 1)

            # Act
            run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)))

            # Assert
            self.assert_notice(run)

    def test_notice_when_cli_output_is_not_one_json_document(self):
        outputs = (
            ("truncated", '{"ok": true, "memories": ['),
            ("empty", ""),
            ("plain text", "warning: something went wrong"),
            ("two documents", self.SEARCH_RESULT + "\n" + self.SEARCH_RESULT),
        )
        for label, stdout in outputs:
            with self.subTest(output=label):
                # Arrange
                stub = self.make_output_stub(f"broken-python-{len(stdout)}", stdout, 0)

                # Act
                run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)))

                # Assert
                self.assert_notice(run)

    def test_notice_when_cli_response_is_not_ok_despite_exit_zero(self):
        responses = (
            ("ok is false", search_response(ok=False)),
            ("ok is the string true", search_response(ok="true")),
            ("ok is missing", {"memories": search_response()["memories"], "count": 1}),
        )
        for index, (label, response) in enumerate(responses):
            with self.subTest(response=label):
                # Arrange
                stub = self.make_output_stub(
                    f"not-ok-python-{index}", json.dumps(response, ensure_ascii=False), 0
                )

                # Act
                run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)))

                # Assert
                self.assert_notice(run)

    def test_notice_when_ok_response_has_malformed_memories(self):
        valid = search_response()["memories"][0]
        without_id = {name: value for name, value in valid.items() if name != "id"}
        without_summary = {name: value for name, value in valid.items() if name != "summary"}
        responses = (
            ("memories is missing", {"ok": True, "count": 1}),
            ("memories is null", search_response(memories=None)),
            ("memories is false", search_response(memories=False)),
            ("memories is a string", search_response(memories="x")),
            ("memories is a number", search_response(memories=1)),
            ("memories is an object", search_response(memories={})),
            ("memory is null", search_response(memories=[None])),
            ("memory is a string", search_response(memories=["global/philosophy-stub"])),
            ("summary is missing", search_response(memories=[without_summary])),
            ("summary is null", search_response(memories=[dict(valid, summary=None)])),
            ("summary is a number", search_response(memories=[dict(valid, summary=1)])),
            ("summary is an array", search_response(memories=[dict(valid, summary=["x"])])),
            ("summary is an object", search_response(memories=[dict(valid, summary={})])),
            ("id is missing", search_response(memories=[without_id])),
            ("id is a number", search_response(memories=[dict(valid, id=1)])),
            ("id is an array", search_response(memories=[dict(valid, id=["global/x"])])),
            ("id is an object", search_response(memories=[dict(valid, id={})])),
            (
                "a valid memory next to a malformed one",
                search_response(memories=[valid, dict(valid, id=None)]),
            ),
        )
        for index, (label, response) in enumerate(responses):
            with self.subTest(response=label):
                # Arrange
                stub = self.make_output_stub(
                    f"malformed-python-{index}", json.dumps(response, ensure_ascii=False), 0
                )

                # Act
                run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)))

                # Assert
                self.assert_notice(run)

    @unittest.skipUnless(PS_PATH, "ps is not available to inspect leftover processes")
    def test_notice_and_no_leftover_processes_when_cli_outlives_hook_timeout(self):
        # Arrange
        stub = self.make_sleeping_stub()

        # Act
        run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub), LLM_MEMORY_HOOK_TIMEOUT="1"))

        # Assert
        self.assert_notice(run)
        self.assertLess(run.elapsed, 4.0)
        self.assertEqual(
            self.wait_for_session_to_empty(self.hook_sessions[-1]),
            [],
            "processes started by the CLI outlived the hook's time limit",
        )

    def test_notice_when_store_lock_is_held_longer_than_hook_timeout(self):
        # Arrange
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        holder_code = textwrap.dedent(
            """\
            import sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from store_lock import store_lock
            with store_lock(Path(sys.argv[2])):
                print("locked", flush=True)
                time.sleep(120)
            """
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(self.memory_dir), str(self.vault)],
            env=self.hook_env(),
            cwd=self.workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.communicate)
        self.addCleanup(holder.kill)
        if holder.stdout is None:
            self.fail("lock holder has no stdout pipe")
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        probe_code = textwrap.dedent(
            """\
            import sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from store_lock import store_lock
            try:
                with store_lock(Path(sys.argv[2]), timeout=0.2):
                    sys.exit(1)
            except TimeoutError:
                sys.exit(0)
            """
        )
        probe = subprocess.run(
            [sys.executable, "-c", probe_code, str(self.memory_dir), str(self.vault)],
            env=self.hook_env(),
            cwd=self.workdir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, f"store lock is not held: {probe.stderr}")

        # Act
        run = self.run_hook(self.hook_env(LLM_MEMORY_HOOK_TIMEOUT="1"))

        # Assert
        self.assert_notice(run)
        self.assertLess(run.elapsed, 4.0)

    @unittest.skipUnless(PS_PATH, "ps is not available to inspect leftover processes")
    def test_notice_and_no_leftover_processes_when_building_context_outlives_hook_timeout(self):
        # Arrange
        slow_bin = self.make_delayed_jq_bin(SLEEPING_STUB_SECONDS, name="bin-with-slow-jq")
        stub = self.make_output_stub("ok-python-for-slow-jq", self.SEARCH_RESULT, 0)

        with self.subTest(control="the same output is injected with a prompt jq"):
            run = self.run_hook(
                self.hook_env(LLM_MEMORY_PYTHON=str(stub), LLM_MEMORY_HOOK_TIMEOUT="1")
            )
            self.assert_injected(run, expected_context([STUB_ENTRY]))

        # Act
        run = self.run_hook(
            self.hook_env(
                PATH=f"{slow_bin}:{MINIMAL_PATH}",
                LLM_MEMORY_PYTHON=str(stub),
                LLM_MEMORY_HOOK_TIMEOUT="1",
            ),
            deadline=SHORT_RUN_DEADLINE_SECONDS,
        )

        # Assert
        self.assert_notice(run)
        self.assertLess(run.elapsed, 4.0)
        self.assertEqual(
            self.wait_for_session_to_empty(self.hook_sessions[-1]),
            [],
            "jq started by the hook outlived the hook's time limit",
        )

    @unittest.skipUnless(PS_PATH, "ps is not available to inspect leftover processes")
    def test_cli_and_building_the_context_share_one_hook_timeout(self):
        # Arrange
        delayed_bin = self.make_delayed_jq_bin(JQ_DELAY_SECONDS)
        response_file = self.root / "stubs" / "delayed-response.json"
        response_file.parent.mkdir(exist_ok=True)
        response_file.write_text(self.SEARCH_RESULT, encoding="utf-8")
        stub = self.make_stub(
            "delayed-python",
            f"sleep {CLI_DELAY_SECONDS}\ncat {shlex.quote(str(response_file))}\n",
        )

        def run_with_limit(limit: str) -> HookRun:
            return self.run_hook(
                self.hook_env(
                    PATH=f"{delayed_bin}:{MINIMAL_PATH}",
                    LLM_MEMORY_PYTHON=str(stub),
                    LLM_MEMORY_HOOK_TIMEOUT=limit,
                ),
                deadline=SHORT_RUN_DEADLINE_SECONDS,
            )

        with self.subTest(control="a limit longer than both parts together injects"):
            self.assert_injected(run_with_limit("3"), expected_context([STUB_ENTRY]))

        # Act
        run = run_with_limit("1")

        # Assert
        self.assert_notice(run)
        self.assertLess(run.elapsed, 4.0)
        self.assertEqual(
            self.wait_for_session_to_empty(self.hook_sessions[-1]),
            [],
            "work started under the hook's time limit outlived it",
        )

    def test_default_hook_timeout_is_five_seconds(self):
        # Arrange
        stub = self.make_sleeping_stub()

        # Act
        run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub)))

        # Assert
        self.assert_notice(run)
        self.assertGreaterEqual(run.elapsed, DEFAULT_TIMEOUT_MIN_ELAPSED)
        self.assertLess(run.elapsed, DEFAULT_TIMEOUT_MAX_ELAPSED)

    def test_hook_timeout_of_nine_seconds_uses_five_second_default(self):
        # Arrange: one out-of-range value is enough to measure; every invalid value is
        # also compared with the default through the arguments passed to timeout.
        stub = self.make_sleeping_stub()

        # Act
        run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub), LLM_MEMORY_HOOK_TIMEOUT="9"))

        # Assert
        self.assert_notice(run)
        self.assertGreaterEqual(run.elapsed, DEFAULT_TIMEOUT_MIN_ELAPSED)
        self.assertLess(run.elapsed, DEFAULT_TIMEOUT_MAX_ELAPSED)

    def test_hook_timeout_of_eight_seconds_is_honored(self):
        # Arrange
        stub = self.make_sleeping_stub()

        # Act
        run = self.run_hook(self.hook_env(LLM_MEMORY_PYTHON=str(stub), LLM_MEMORY_HOOK_TIMEOUT="8"))

        # Assert
        self.assert_notice(run)
        self.assertGreaterEqual(run.elapsed, 7.5)
        self.assertLess(run.elapsed, 11.0)


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
class TestHookTimeoutValueWithHealthyCli(HookTestBase):
    EXPECTED = expected_context([("一つのことをうまくやる", "global/philosophy-one-thing")])

    def setUp(self):
        super().setUp()
        self.seed("philosophy-one-thing", "一つのことをうまくやる")

    def test_invalid_hook_timeout_does_not_prevent_injection(self):
        values = ("abc", "-1", "1abc", "0", "1.5", "", "9", "99999999999999999999", "５")
        for value in values:
            with self.subTest(LLM_MEMORY_HOOK_TIMEOUT=value):
                # Act
                run = self.run_hook(self.hook_env(LLM_MEMORY_HOOK_TIMEOUT=value))

                # Assert
                self.assert_injected(run, self.EXPECTED)

    def test_full_width_digit_hook_timeout_is_invalid_in_every_locale(self):
        for label, extra_env in LOCALE_VARIANTS:
            with self.subTest(locale=label):
                if not locale_is_usable(extra_env):
                    self.skipTest(f"{label} is not available on this machine")

                # Act
                run = self.run_hook(self.hook_env(LLM_MEMORY_HOOK_TIMEOUT="５", **extra_env))

                # Assert
                self.assert_injected(run, self.EXPECTED)


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
@unittest.skipUnless(JQ_PATH, "jq is not on PATH")
@unittest.skipUnless(REAL_TIMEOUT, "no working timeout command on the minimal PATH")
class TestHookTimeoutCommandLookup(HookTestBase):
    EXPECTED = expected_context([("一つのことをうまくやる", "global/philosophy-one-thing")])

    def setUp(self):
        super().setUp()
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        self.timeout_calls = self.root / "timeout-calls"
        self.bin_dir = self.make_bin_without_timeout_commands()

    def make_bin_without_timeout_commands(self) -> Path:
        # Everything on the minimal PATH except timeout/gtimeout is linked, so the
        # hook can use any other ordinary command it needs.
        bin_dir = self.root / "bin-without-timeout"
        bin_dir.mkdir()
        for directory in MINIMAL_PATH.split(":"):
            for entry in sorted(Path(directory).iterdir()):
                link = bin_dir / entry.name
                if entry.name in TIMEOUT_COMMAND_NAMES or os.path.lexists(link):
                    continue
                if entry.is_file() and os.access(entry, os.X_OK):
                    link.symlink_to(entry)
        if JQ_PATH:
            (bin_dir / "jq").symlink_to(JQ_PATH)
        probe = _run_probe(
            ["/bin/bash", "-c", "command -v timeout || command -v gtimeout"],
            {"PATH": str(bin_dir)},
        )
        self.assertNotEqual(probe.returncode, 0, f"a timeout command is still reachable: {probe}")
        self.assertEqual(
            _run_probe(["/bin/bash", "-c", "command -v jq"], {"PATH": str(bin_dir)}).returncode, 0
        )
        return bin_dir

    def add_recording_timeout(self, name: str) -> None:
        assert REAL_TIMEOUT is not None
        path = self.bin_dir / name
        path.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' {shlex.quote(name)} >> {shlex.quote(str(self.timeout_calls))}\n"
            f'exec {shlex.quote(REAL_TIMEOUT)} "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)

    def recorded_timeout_calls(self) -> list[str]:
        if not self.timeout_calls.exists():
            return []
        return self.timeout_calls.read_text(encoding="utf-8").split()

    def test_uses_gtimeout_when_timeout_is_not_on_path(self):
        # Arrange
        self.add_recording_timeout("gtimeout")

        # Act
        run = self.run_hook(self.hook_env(PATH=str(self.bin_dir)))

        # Assert
        self.assert_injected(run, self.EXPECTED)
        calls = self.recorded_timeout_calls()
        self.assertTrue(calls, "the CLI was not run through gtimeout")
        self.assertEqual(set(calls), {"gtimeout"})

    def test_prefers_timeout_when_both_timeout_and_gtimeout_are_on_path(self):
        # Arrange
        self.add_recording_timeout("timeout")
        self.add_recording_timeout("gtimeout")

        # Act
        run = self.run_hook(self.hook_env(PATH=str(self.bin_dir)))

        # Assert
        self.assert_injected(run, self.EXPECTED)
        calls = self.recorded_timeout_calls()
        self.assertTrue(calls, "the CLI was not run through timeout")
        self.assertEqual(set(calls), {"timeout"})

    def test_invalid_hook_timeout_passes_the_same_time_limit_as_when_unset(self):
        # Arrange: GNU timeout itself accepts "0" (no limit), "1.5" and huge values, so
        # injecting successfully does not show that such values were replaced. Only the
        # duration is compared; the other arguments may differ from run to run.
        assert REAL_TIMEOUT is not None
        options_taking_a_value = {"-s", "--signal", "-k", "--kill-after"}
        arguments_file = self.root / "timeout-arguments"
        wrapper = self.bin_dir / "timeout"
        wrapper.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\0' \"$@\" > {shlex.quote(str(arguments_file))}\n"
            f'exec {shlex.quote(REAL_TIMEOUT)} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        def duration_passed_to_timeout(value: str | None) -> str:
            with suppress(FileNotFoundError):
                arguments_file.unlink()
            run = self.run_hook(
                self.hook_env(PATH=str(self.bin_dir), LLM_MEMORY_HOOK_TIMEOUT=value)
            )
            self.assert_injected(run, self.EXPECTED)
            self.assertTrue(arguments_file.exists(), "the CLI was not run through timeout")
            arguments = arguments_file.read_text(encoding="utf-8").split("\0")[:-1]
            index = 0
            while index < len(arguments) and arguments[index].startswith("-"):
                index += 2 if arguments[index] in options_taking_a_value else 1
            self.assertLess(index, len(arguments), f"no duration in {arguments}")
            return arguments[index]

        unset = duration_passed_to_timeout(None)

        for value in ("4", "8"):
            with self.subTest(valid=value):
                # Act / Assert
                self.assertNotEqual(duration_passed_to_timeout(value), unset)

        for value in ("abc", "0", "-1", "9", "99999999999999999999", "1.5", "", "1abc", "５"):
            with self.subTest(invalid=value):
                # Act / Assert
                self.assertEqual(duration_passed_to_timeout(value), unset)

    def test_notice_without_running_cli_when_no_timeout_command_exists(self):
        # Arrange
        cli_marker = self.root / "cli-was-run"
        stub = self.make_stub(
            "marking-python",
            f"""\
            : > {shlex.quote(str(cli_marker))}
            exec {shlex.quote(sys.executable)} "$@"
            """,
        )

        # Act
        run = self.run_hook(self.hook_env(PATH=str(self.bin_dir), LLM_MEMORY_PYTHON=str(stub)))

        # Assert
        self.assert_notice(run)
        self.assertFalse(cli_marker.exists(), "the CLI must not run without a time limit")


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML cannot be imported by sys.executable")
class TestHookWithoutJq(HookTestBase):
    def test_notice_is_valid_json_when_jq_is_not_on_path(self):
        # Arrange
        self.seed("philosophy-one-thing", "一つのことをうまくやる")
        env = self.hook_env(PATH=MINIMAL_PATH)
        if _run_probe(["/bin/bash", "-c", "command -v jq"], env).returncode == 0:
            self.skipTest(f"jq is reachable from {MINIMAL_PATH}")

        # Act
        run = self.run_hook(env)

        # Assert
        self.assert_notice(run)


if __name__ == "__main__":
    unittest.main()
