#!/usr/bin/env python3
"""hook-post-secret-scan.sh（書き込み後にシークレットのハードコードを警告する PostToolUse フック）の契約テスト。

フックは payload の file_path が指すファイルを読むだけで、何も書き込まない。テストは
HOME を一時ディレクトリへ差し替え、環境を継承せずに実行する。jq の有無を切り替えるため、
PATH には /usr/bin と /bin から jq を除いた写しと、ケースごとの bin だけを置く。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
REAL_HOME = Path.home().resolve()

HOOK_NAME = "hook-post-secret-scan.sh"
# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
HOOK_UNDER_TEST = Path(
    os.environ.get("HOOK_POST_SECRET_SCAN_TARGET") or REPO_ROOT / ".claude" / "scripts" / HOOK_NAME
)

# フックが実行中に書き換えてはならない実データ。
REAL_DATA_PATHS = (
    REPO_ROOT / ".claude" / "scripts",
    REAL_HOME / ".claude" / "settings.json",
)

MINIMAL_PATH = "/usr/bin:/bin"
RUN_DEADLINE_SECONDS = 30.0
SWITCHED_COMMANDS = frozenset({"jq"})
SCAN_FAILED_NEEDLE = "シークレットスキャン"
# 検出される代入の行。このテストファイル自体がスキャンに反応しないよう、実行時に組み立てる。
HARD_CODED_ASSIGNMENT = "api_key" + ' = "abcd1234efgh5678"\n'


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
GREP_PATH = shutil.which("grep", path=MINIMAL_PATH)
MKDIR_PATH = shutil.which("mkdir", path=MINIMAL_PATH)

# シークレットの行に混ぜる、UTF-8 として不正なバイト列や NUL。
NON_UTF8_SUFFIXES: dict[str, bytes] = {
    "a Latin-1 byte": b"  # caf" + bytes([0xE9]),
    "Shift_JIS text": b"  # " + "東京".encode("shift_jis"),
    "a NUL byte": b"  # a" + bytes([0]) + b"b",
}
LOCALE_VARIANTS: dict[str, dict[str, str]] = {
    "C": {"LANG": "C", "LC_ALL": "C"},
    "ja_JP.UTF-8": {"LANG": "ja_JP.UTF-8", "LC_ALL": "ja_JP.UTF-8"},
}


def locale_is_usable(variables: dict[str, str]) -> bool:
    """ロケールは名前ではなく挙動で判定する: C 以外は UTF-8 として "あい" を 2 文字と数える。"""
    expected = "6" if variables.get("LC_ALL") == "C" else "2"
    result = subprocess.run(
        ["/bin/bash", "-c", 'x="あい"; printf "%s" "${#x}"'],
        env={"PATH": MINIMAL_PATH, **variables},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout == expected and result.stderr == ""


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


def build_command_tree(destination: Path, excluded: frozenset[str] = frozenset()) -> None:
    """/usr/bin と /bin の実行ファイルへのリンクを、切り替え対象と excluded のコマンドを除いて並べる。"""
    destination.mkdir(parents=True)
    for directory in (Path("/usr/bin"), Path("/bin")):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            link = destination / entry.name
            if entry.name in SWITCHED_COMMANDS | excluded or os.path.lexists(link):
                continue
            if entry.is_file() and os.access(entry, os.X_OK):
                link.symlink_to(entry)


@dataclass
class HookRun:
    returncode: int
    stdout: str
    stderr: str


@unittest.skipUnless(JQ_PATH, "a working jq is not available")
class HookPostSecretScanTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="hook-post-secret-scan-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        abort_if_unsafe_temp_root(self.root)

        self.hook = self.root / "copy" / HOOK_NAME
        self.hook.parent.mkdir(parents=True)
        shutil.copy2(HOOK_UNDER_TEST, self.hook)

        self.home = self.root / "home"
        self.project = self.root / "project"
        for directory in (self.home, self.project):
            directory.mkdir(parents=True)
        self.commands = self.root / "commands"
        build_command_tree(self.commands)
        self.variant_count = 0

    # -- fixtures ----------------------------------------------------------

    def write_project_file(self, name: str, content: str) -> Path:
        path = self.project / name
        path.write_text(content, encoding="utf-8")
        return path

    def write_stub(self, path: Path, body: str) -> None:
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def commands_without(self, excluded: frozenset[str]) -> Path:
        """切り替え対象に加えて excluded のコマンドも除いた写し。"""
        if not excluded:
            return self.commands
        tree = self.root / ("commands-without-" + "-".join(sorted(excluded)))
        if not tree.exists():
            build_command_tree(tree, excluded)
        return tree

    def hook_env(self, *, jq: str = "real", without: frozenset[str] = frozenset()) -> dict[str, str]:
        assert JQ_PATH is not None
        self.variant_count += 1
        bin_dir = self.root / f"variant-bin-{self.variant_count}"
        bin_dir.mkdir()
        if jq == "real":
            (bin_dir / "jq").symlink_to(JQ_PATH)
        elif jq == "failing":
            self.write_stub(bin_dir / "jq", "exit 1\n")
        elif jq == "fails-with-7":
            # 事前確認（-n）は本物として通し、payload を読む呼び出しだけ終了コード 7 で失敗する。
            self.write_stub(
                bin_dir / "jq",
                f'for a in "$@"; do [ "$a" = -n ] && exec "{JQ_PATH}" "$@"; done\nexit 7\n',
            )
        elif jq != "absent":
            raise ValueError(jq)
        return {
            "HOME": str(self.home),
            "PATH": f"{bin_dir}:{self.commands_without(without)}",
            "CLAUDE_PROJECT_DIR": str(self.project),
        }

    def payload(self, file_path: Path | None) -> str:
        data: dict[str, object] = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": str(self.project),
            "tool_input": {} if file_path is None else {"file_path": str(file_path)},
        }
        return json.dumps(data, ensure_ascii=False)

    # -- running -----------------------------------------------------------

    @contextmanager
    def real_environment_unchanged(self) -> Iterator[None]:
        before = {path: snapshot_tree(path) for path in REAL_DATA_PATHS}
        for path, entries in before.items():
            self.assertIsNotNone(entries, f"snapshot of {path} could not be collected")
        yield
        for path in REAL_DATA_PATHS:
            self.assertEqual(snapshot_tree(path), before[path], f"{path} changed during the run")

    def abort_if_environment_escapes_tree(self, env: dict[str, str], hook: Path) -> None:
        if Path(env["HOME"]).resolve() == REAL_HOME or not hook.resolve().is_relative_to(
            self.root
        ):
            sys.stderr.write("aborting: environment escapes the test tree\n")
            sys.stderr.flush()
            os._exit(3)

    def run_hook(
        self,
        stdin: str,
        *,
        jq: str = "real",
        without: frozenset[str] = frozenset(),
        stubs: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> HookRun:
        env = self.hook_env(jq=jq, without=without)
        bin_dir = Path(env["PATH"].split(":")[0])
        for name, body in (stubs or {}).items():
            self.write_stub(bin_dir / name, body)
        env.update(extra_env or {})
        self.abort_if_environment_escapes_tree(env, self.hook)
        with self.real_environment_unchanged():
            try:
                result = subprocess.run(
                    [str(self.hook)],
                    input=stdin.encode("utf-8"),
                    capture_output=True,
                    env=env,
                    cwd=str(self.project),
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

    def assert_silent(self, run: HookRun) -> None:
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stdout, "", "nothing to report must produce no output")
        self.assertEqual(run.stderr, "", "the hook must not write to stderr")

    def assert_reported_scan_failure(self, run: HookRun, needles: tuple[str, ...] = ()) -> None:
        # PostToolUse の exit 2 は書き込みを取り消せないが、stderr が Claude に表示される。
        self.assertEqual(run.returncode, 2, f"stdout: {run.stdout!r} stderr: {run.stderr!r}")
        self.assertIn(SCAN_FAILED_NEEDLE, run.stderr)
        for needle in needles:
            self.assertIn(needle, run.stderr)


class TestScanResults(HookPostSecretScanTestBase):
    def test_warns_when_a_secret_is_hard_coded(self) -> None:
        # Arrange
        target = self.write_project_file("config.py", HARD_CODED_ASSIGNMENT)

        # Act
        run = self.run_hook(self.payload(target))

        # Assert
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr!r}")
        self.assertEqual(run.stderr, "")
        decision = json.loads(run.stdout)
        output = decision["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PostToolUse")
        self.assertIn("WARNING", output["additionalContext"])
        self.assertIn(str(target), output["additionalContext"])

    def test_stays_silent_when_no_secret_is_found(self) -> None:
        cases = {
            "a dynamic reference": "password = ${PASSWORD}\n",
            "a placeholder": 'api_key = "<YOUR_API_KEY>"\n',
            "no assignment at all": "print('hello')\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                # Arrange
                target = self.write_project_file("clean.txt", content)

                # Act / Assert
                self.assert_silent(self.run_hook(self.payload(target)))

    def test_stays_silent_when_there_is_nothing_to_scan(self) -> None:
        cases: dict[str, Path | None] = {
            "file_path missing": None,
            "the file does not exist": self.project / "missing.py",
            "file_path is a directory": self.project,
        }
        for label, target in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_silent(self.run_hook(self.payload(target)))


class TestScanFailures(HookPostSecretScanTestBase):
    def test_reports_that_the_scan_could_not_run_when_jq_does_not_work(self) -> None:
        # Arrange
        leaky = self.write_project_file("config.py", HARD_CODED_ASSIGNMENT)
        payloads = {
            "a file with a secret": self.payload(leaky),
            "a payload without file_path": self.payload(None),
        }
        for jq in ("absent", "failing"):
            for label, stdin in payloads.items():
                with self.subTest(jq=jq, case=label):
                    # Act
                    run = self.run_hook(stdin, jq=jq)

                    # Assert
                    self.assert_reported_scan_failure(run, ("jq",))

    def test_reports_that_the_scan_could_not_run_when_the_payload_is_broken(self) -> None:
        cases = {
            "not json": "not json at all",
            "truncated json": '{"tool_input": {"file_path": "',
        }
        for label, stdin in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_reported_scan_failure(self.run_hook(stdin))


class TestScanCannotComplete(HookPostSecretScanTestBase):
    """jq 以外の判定に使うコマンド（cat・grep）が動かない場合や、対象を読めない場合も、
    スキャンを最後まで実行できなかったことを伝える。
    """

    def test_reports_that_the_scan_could_not_run_when_the_payload_cannot_be_read(self) -> None:
        # Arrange
        leaky = self.write_project_file("config.py", HARD_CODED_ASSIGNMENT)

        # Act
        run = self.run_hook(self.payload(leaky), without=frozenset({"cat"}))

        # Assert
        self.assert_reported_scan_failure(run)

    def test_reports_that_the_scan_could_not_run_without_grep(self) -> None:
        cases = {
            "a file with a secret": HARD_CODED_ASSIGNMENT,
            "a file without a secret": "print('hello')\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                # Arrange
                target = self.write_project_file("scanned.py", content)

                # Act
                run = self.run_hook(self.payload(target), without=frozenset({"grep"}))

                # Assert
                self.assert_reported_scan_failure(run)

    def test_reports_that_the_scan_could_not_run_when_the_file_is_unreadable(self) -> None:
        # Arrange
        target = self.write_project_file("unreadable.py", HARD_CODED_ASSIGNMENT)
        target.chmod(0o200)
        self.addCleanup(target.chmod, 0o600)
        if os.access(target, os.R_OK):
            self.skipTest("the file stays readable (running with elevated privileges)")

        # Act
        run = self.run_hook(self.payload(target))

        # Assert
        self.assert_reported_scan_failure(run, (str(target),))

    def test_includes_the_jq_exit_status_in_the_reason(self) -> None:
        # Arrange: 壊れた JSON に対する jq の終了コードは、実際の jq に確かめさせる。
        assert JQ_PATH is not None
        broken = "not json at all"
        probe = subprocess.run(
            [JQ_PATH, "-r", "."],
            input=broken,
            env={"PATH": MINIMAL_PATH},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(probe.returncode, 0)
        cases = {
            "the real jq on broken json": (broken, "real", probe.returncode),
            "a jq that fails with 7": (self.payload(None), "fails-with-7", 7),
        }
        for label, (stdin, jq, status) in cases.items():
            with self.subTest(case=label):
                # Act
                run = self.run_hook(stdin, jq=jq)

                # Assert
                self.assert_reported_scan_failure(run, ("終了コード",))
                self.assertRegex(run.stderr, rf"(?<![0-9]){status}(?![0-9])")

    def failing_grep_stub(self, calls_dir: Path, condition: str) -> str:
        """呼び出しごとに通し番号を振り、condition が成り立つ呼び出しだけ exit 2 で失敗する grep。

        パイプラインの各段は並行に起動されるため、番号は mkdir の成否で原子的に取る。
        失敗させた呼び出しは calls_dir/failed に記録し、実装が何回 grep を呼ぶかに頼らずに判定する。
        """
        assert GREP_PATH is not None and MKDIR_PATH is not None
        calls_dir.mkdir()
        return (
            f'[ -d "{calls_dir}" ] || exit 99\n'
            "i=1\n"
            f'until "{MKDIR_PATH}" "{calls_dir}/$i" 2>/dev/null; do\n'
            '  i=$((i + 1)); [ "$i" -le 100 ] || exit 99\n'
            "done\n"
            f'if {condition}; then : > "{calls_dir}/failed"; exit 2; fi\n'
            f'exec "{GREP_PATH}" "$@"\n'
        )

    def test_reports_that_the_scan_could_not_run_when_any_grep_call_fails(self) -> None:
        # Arrange: どの呼び出しの失敗も「一致なし」として扱ってはならない。
        target = self.write_project_file("config.py", HARD_CODED_ASSIGNMENT)
        conditions = {
            "only the 1st call": '[ "$i" -eq 1 ]',
            "only the 2nd call": '[ "$i" -eq 2 ]',
            "only the 3rd call": '[ "$i" -eq 3 ]',
            "the 2nd call onward": '[ "$i" -ge 2 ]',
            "a call given -Evi": 'case " $* " in *" -Evi "*) true;; *) false;; esac',
            "a call given -q": 'case " $* " in *" -q "*) true;; *) false;; esac',
        }
        triggered = []
        for label, condition in conditions.items():
            with self.subTest(case=label):
                calls_dir = self.root / f"grep-calls-{len(triggered)}-{abs(hash(label))}"
                stub = self.failing_grep_stub(calls_dir, condition)

                # Act
                run = self.run_hook(
                    self.payload(target), without=frozenset({"grep"}), stubs={"grep": stub}
                )

                # Assert
                if (calls_dir / "failed").exists():
                    triggered.append(label)
                    self.assert_reported_scan_failure(run)
                else:
                    self.assertEqual(run.returncode, 0, run)
                    self.assertIn("WARNING", run.stdout)
        with self.subTest(case="a later call failed at least once"):
            self.assertTrue(
                set(triggered) - {"only the 1st call"},
                f"no call after the first one was ever failed: {triggered}",
            )

    def test_harness_detects_a_hook_that_exits_without_reading_stdin(self) -> None:
        # Arrange
        quitter = self.root / "quitter.sh"
        self.write_stub(quitter, "exit 2\n")

        # Act
        _, write_error = self.feed_hook(
            quitter, large_payload(self.payload(None)), self.hook_env()
        )

        # Assert
        self.assertEqual(write_error, "BrokenPipeError")

    def test_reads_a_large_payload_to_the_end(self) -> None:
        # Arrange
        leaky = self.write_project_file("config.py", HARD_CODED_ASSIGNMENT)
        stdin = large_payload(self.payload(leaky))
        variants: dict[str, str] = {"jq is missing": "absent", "all dependencies work": "real"}
        for label, jq in variants.items():
            with self.subTest(case=label):
                # Act
                run, write_error = self.feed_hook(self.hook, stdin, self.hook_env(jq=jq))

                # Assert
                self.assertEqual(write_error, "", f"the hook did not read stdin: {run!r}")
                if jq == "absent":
                    self.assert_reported_scan_failure(run, ("jq",))
                else:
                    self.assertEqual(run.returncode, 0, run)
                    self.assertIn("WARNING", run.stdout)

    # -- running -----------------------------------------------------------

    def feed_hook(self, hook: Path, stdin: bytes, env: dict[str, str]) -> tuple[HookRun, str]:
        """stdin を別スレッドで書き込みながら実行し、書き込みの失敗（パイプの切断）も返す。

        communicate() は書き込み中の BrokenPipeError を握りつぶすため、ここでは自前で書く。
        パイプの容量（Linux では 64KiB）を超える入力は、相手が読まない限り書き終わらない。
        """
        self.abort_if_environment_escapes_tree(env, hook)
        with self.real_environment_unchanged():
            process = subprocess.Popen(
                [str(hook)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(self.project),
                start_new_session=True,
            )
            outputs: dict[str, bytes] = {}
            write_error: list[str] = []

            def write() -> None:
                assert process.stdin is not None
                try:
                    process.stdin.write(stdin)
                    process.stdin.close()
                except OSError as error:
                    write_error.append(type(error).__name__)

            def read(name: str) -> None:
                outputs[name] = getattr(process, name).read()

            threads = [threading.Thread(target=write)] + [
                threading.Thread(target=read, args=(name,)) for name in ("stdout", "stderr")
            ]
            for thread in threads:
                thread.start()
            try:
                process.wait(timeout=RUN_DEADLINE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                self.fail(f"the hook did not finish within {RUN_DEADLINE_SECONDS} seconds")
            finally:
                for thread in threads:
                    thread.join(timeout=10)
        run = HookRun(
            process.returncode,
            outputs.get("stdout", b"").decode("utf-8", "replace"),
            outputs.get("stderr", b"").decode("utf-8", "replace"),
        )
        return run, ",".join(write_error)


def large_payload(payload: str) -> bytes:
    data = json.loads(payload)
    data["padding"] = "x" * (1 << 20)
    return json.dumps(data).encode("utf-8")


class TestNonUtf8Content(HookPostSecretScanTestBase):
    """シークレットの行に UTF-8 として不正なバイトや NUL があっても、ロケールに関わらず警告する。
    UTF-8 ロケールの grep はそうした行をバイナリとみなし、照合結果を出さずに成功で終わりうる。
    """

    def test_warns_about_a_secret_on_a_line_with_non_utf8_bytes(self) -> None:
        for locale_label, variables in LOCALE_VARIANTS.items():
            usable = locale_is_usable(variables)
            for content_label, suffix in NON_UTF8_SUFFIXES.items():
                with self.subTest(locale=locale_label, content=content_label):
                    if not usable:
                        self.skipTest(f"the {locale_label} locale is not available")
                    # Arrange
                    target = self.project / "mixed.py"
                    target.write_bytes(HARD_CODED_ASSIGNMENT.rstrip("\n").encode() + suffix + b"\n")

                    # Act
                    run = self.run_hook(self.payload(target), extra_env=variables)

                    # Assert
                    self.assertEqual(run.returncode, 0, run)
                    self.assertEqual(run.stderr, "")
                    self.assertIn("WARNING", run.stdout, "no warning for the secret")
                    output = json.loads(run.stdout)["hookSpecificOutput"]
                    self.assertIn("WARNING", output["additionalContext"])

    def test_stays_silent_on_an_excluded_line_with_non_utf8_bytes(self) -> None:
        # Arrange: 除外パターンに当たる行は、不正なバイトがあっても除外されたままになる。
        excluded_line = b"password = ${PASSWORD}" + NON_UTF8_SUFFIXES["Shift_JIS text"] + b"\n"
        for locale_label, variables in LOCALE_VARIANTS.items():
            with self.subTest(locale=locale_label):
                if not locale_is_usable(variables):
                    self.skipTest(f"the {locale_label} locale is not available")
                target = self.project / "excluded.py"
                target.write_bytes(excluded_line)

                # Act / Assert
                self.assert_silent(self.run_hook(self.payload(target), extra_env=variables))

    def test_warns_about_an_unquoted_value_that_starts_with_non_utf8_bytes(self) -> None:
        # Arrange: UTF-8 ロケールでは不正なバイト列が \S に当たらず、値の先頭にあると照合から漏れうる。
        lines = {
            "Latin-1": b"db." + b"password" + b"=" + bytes([0xE9]) + b"lan2024\n",
            "Shift_JIS": b"password" + b"=" + "東京".encode("shift_jis") + b"2024\n",
        }
        for locale_label, variables in LOCALE_VARIANTS.items():
            usable = locale_is_usable(variables)
            for content_label, line in lines.items():
                with self.subTest(locale=locale_label, content=content_label):
                    if not usable:
                        self.skipTest(f"the {locale_label} locale is not available")
                    target = self.project / "unquoted.properties"
                    target.write_bytes(line)

                    # Act
                    run = self.run_hook(self.payload(target), extra_env=variables)

                    # Assert
                    self.assertEqual(run.returncode, 0, run)
                    self.assertEqual(run.stderr, "")
                    self.assertIn("WARNING", run.stdout, "no warning for the secret")

    def locale_sensitive_lines(self) -> dict[str, bytes]:
        """ロケールによって照合結果が変わりうる行。警告の有無は問わず、両ロケールで同じ結果を求める。"""
        lines: dict[str, bytes] = {}
        for label, raw in (
            ("Latin-1", bytes([0xE9])),
            ("Shift_JIS", "東京".encode("shift_jis")),
            ("NUL", bytes([0])),
        ):
            lines[f"{label} at the head of an unquoted value"] = (
                b"password" + b"=" + raw + b"lan2024\n"
            )
            lines[f"{label} in the middle of a quoted value"] = (
                b"api_key" + b' = "abcd' + raw + b'5678"\n'
            )
            lines[f"{label} at the end of the line"] = (
                HARD_CODED_ASSIGNMENT.rstrip("\n").encode() + b"  # " + raw + b"\n"
            )
            lines[f"{label} on an excluded line"] = b"password" + b" = ${PASSWORD} # " + raw + b"\n"
        # 引き換えとして受け入れた入力。警告の有無は固定せず、ロケールで変わらないことだけを見る。
        lines["a full-width space before ="] = b"password" + "　".encode() + b'= "abcd1234"\n'
        lines["smart quotes around a placeholder"] = (
            b"password" + b" = " + "“example”".encode() + b"\n"
        )
        return lines

    def test_gives_the_same_result_in_c_and_utf8_locales(self) -> None:
        for label, variables in LOCALE_VARIANTS.items():
            if not locale_is_usable(variables):
                self.skipTest(f"the {label} locale is not available")
        c_locale, utf8_locale = LOCALE_VARIANTS["C"], LOCALE_VARIANTS["ja_JP.UTF-8"]
        for label, line in self.locale_sensitive_lines().items():
            with self.subTest(content=label):
                # Arrange
                target = self.project / "locale.txt"
                target.write_bytes(line)

                # Act
                in_c = self.run_hook(self.payload(target), extra_env=c_locale)
                in_utf8 = self.run_hook(self.payload(target), extra_env=utf8_locale)

                # Assert
                self.assertEqual(in_c.stderr, "")
                self.assertEqual(in_utf8, in_c)


class TestHarness(HookPostSecretScanTestBase):
    def test_jq_is_only_reachable_through_the_variant_bin(self) -> None:
        # Act
        probe = subprocess.run(
            ["/bin/bash", "-c", "command -v jq; command -v grep"],
            env=self.hook_env(jq="absent"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        # Assert
        self.assertEqual(probe.stdout, f"{self.commands / 'grep'}\n", probe)


if __name__ == "__main__":
    unittest.main()
