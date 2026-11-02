#!/usr/bin/env python3
"""hook-pre-boundary.sh（書き込み先を検査する PreToolUse フック）の契約テスト。

フックはプロジェクトルートを CLAUDE_PROJECT_DIR（未設定なら作業ディレクトリ）から、
スクラッチパッドの位置を TMPDIR・実行ユーザーの UID・PreToolUse ペイロードの
session_id から決める。テストは HOME・TMPDIR・CLAUDE_PROJECT_DIR をすべて一時
ディレクトリへ差し替え、環境を継承せずに実行する。フックは読み出し専用だが、
実環境が変化していないことを実行の前後で比較して確かめる。

判定はフックを実行するシェルの視点で行われるため、パスの表記・UID・最小 PATH・
一時ディレクトリの位置はいずれも Python のリテラルではなく、テストが起動するシェル
自身に問い合わせて決める。Git Bash 上では Python から見たパス（C:\\...）とシェルから
見たパス（/tmp/...）が別表記になり、リテラルで書くとテストが黙って空振りする。
"""

from __future__ import annotations

import functools
import json
import os
import re
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
from typing import Iterator, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
REAL_HOME = Path.home().resolve()

HOOK_NAME = "hook-pre-boundary.sh"
# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
HOOK_UNDER_TEST = Path(
    os.environ.get("HOOK_PRE_BOUNDARY_TARGET") or REPO_ROOT / ".claude" / "scripts" / HOOK_NAME
)

IS_WINDOWS = os.name == "nt"

# フックが実行中に書き換えてはならない実データ。
REAL_DATA_PATHS = tuple(
    dict.fromkeys(
        path
        for path in (
            HOOK_UNDER_TEST.parent.resolve(),
            (REPO_ROOT / ".claude" / "scripts").resolve(),
            REAL_HOME / ".claude" / "settings.json",
        )
        if os.path.lexists(path)
    )
)

RUN_DEADLINE_SECONDS = 30.0

# 実際のスクラッチパッドと同じ形のセッション ID。
SESSION_ID = "d1373fcb-6a5f-4a04-8ea6-7925d5fd7cc3"
OTHER_SESSION_ID = "0f2a91cc-5b3e-4d17-9a60-2c8e14b7f503"

# 有無を切り替えて検証する依存コマンド。同じディレクトリにある他のコマンドを残したまま
# これらだけを外すため、PATH は実在するコマンドを呼び直すラッパーだけで組み立てる。
SWITCHED_COMMANDS = frozenset({"jq", "realpath", "grealpath"})

# フックの出力に現れてはならない文字（C0 制御文字）。
CONTROL_CHARACTERS = frozenset(chr(code) for code in range(0x20))

# 表記そのものを理由に拒否したときの理由文に含まれる語。
UNSUPPORTED_NOTATION_REASON = "対応していないパス表記"

# 空にしてよい環境変数。これ以外を空にする起動は行わない（実環境を基準にさせないため）。
EMPTIABLE_VARIABLES = frozenset({"HOME", "TMPDIR", "CLAUDE_PROJECT_DIR"})

# ファイルシステムのルートを指させてよい環境変数（基点がルートのときの判定を確かめる）。
ROOTABLE_VARIABLES = frozenset({"HOME", "CLAUDE_PROJECT_DIR"})

# フック本体が必要とする外部コマンド。
REQUIRED_TOOLS = ("cat", "tr", "jq", "realpath", "uname")

# 最小 PATH を組み立てるときに包んでおく候補。存在するものだけを包む。
WRAPPABLE_TOOLS = (
    "cat",
    "tr",
    "jq",
    "realpath",
    "uname",
    "cygpath",
    "sed",
    "awk",
    "grep",
    "dirname",
    "basename",
    "readlink",
    "env",
    "expr",
    "cut",
    "head",
    "tail",
    "wc",
    "sort",
    "id",
    "pwd",
    "stat",
    "test",
    "true",
    "false",
)


# -- シェルへの問い合わせ --------------------------------------------------

BASH_PATH = shutil.which("bash")


def _bash(
    script: str, *args: str, cwd: str | Path | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH_PATH is not None
    return subprocess.run(
        [BASH_PATH, "-c", script, "harness", *args],
        capture_output=True,
        text=True,
        cwd=None if cwd is None else str(cwd),
        input=stdin,
    )


def _bash_out(
    script: str, *args: str, cwd: str | Path | None = None, stdin: str | None = None
) -> str:
    result = _bash(script, *args, cwd=cwd, stdin=stdin)
    if result.returncode != 0:
        raise RuntimeError(f"bash -c {script!r} failed ({result.returncode}): {result.stderr!r}")
    return result.stdout.strip()


SHELL_ERROR: str | None = None
SHELL_UNAME = ""
SHELL_IS_MSYS = False
CURRENT_UID = -1
TOOL_PATHS: dict[str, str] = {}
MINIMAL_PATH = ""
SHELL_TMP = "/tmp"
SHELL_VAR_TMP = "/var/tmp"
BASH_POSIX = ""


def _resolve_tools(names: Sequence[str]) -> dict[str, str]:
    script = 'for n in "$@"; do p=$(command -v -- "$n" 2>/dev/null) || p=""; '
    script += "printf '%s\\t%s\\n' \"$n\" \"$p\"; done"
    found: dict[str, str] = {}
    for line in _bash_out(script, *names).splitlines():
        name, _, location = line.partition("\t")
        if location:
            found[name] = location
    return found


if BASH_PATH is None:
    SHELL_ERROR = "bash is not on PATH"
else:
    try:
        SHELL_UNAME = _bash_out("uname -s")
        SHELL_IS_MSYS = SHELL_UNAME.startswith(("MINGW", "MSYS", "CYGWIN"))
        CURRENT_UID = int(_bash_out('printf %s "$EUID"'))
        required = list(REQUIRED_TOOLS) + (["cygpath"] if SHELL_IS_MSYS else [])
        TOOL_PATHS = _resolve_tools(required)
        missing = [name for name in required if name not in TOOL_PATHS]
        if missing:
            SHELL_ERROR = f"the shell cannot resolve {', '.join(missing)}"
        else:
            MINIMAL_PATH = ":".join(
                dict.fromkeys(location.rsplit("/", 1)[0] for location in TOOL_PATHS.values())
            )
            SHELL_TMP = _bash_out("realpath -m -- /tmp")
            SHELL_VAR_TMP = _bash_out("realpath -m -- /var/tmp")
            BASH_POSIX = (
                _bash_out('cygpath -u -- "$1"', BASH_PATH) if SHELL_IS_MSYS else BASH_PATH
            )
    except (RuntimeError, ValueError) as error:  # pragma: no cover - 環境不備の説明用
        SHELL_ERROR = str(error)

SHELL_READY = SHELL_ERROR is None
SHELL_SKIP_REASON = SHELL_ERROR or ""

# シェルから見た jq の位置。スタブから本物を呼び直すときに使う。
JQ_PATH: str | None = TOOL_PATHS.get("jq")


def _gnu_realpath() -> str | None:
    """名前ではなく挙動で判定する: 存在しないパスを -m で正規化できる realpath。"""
    path = TOOL_PATHS.get("realpath")
    if not SHELL_READY or path is None:
        return None
    result = _bash('"$1" -m /nonexistent-probe/a/../b', path)
    return path if result.returncode == 0 and result.stdout == "/nonexistent-probe/b\n" else None


# シェルから見た、-m を受け付ける realpath の位置。
GNU_REALPATH = _gnu_realpath()


@functools.lru_cache(maxsize=None)
def wrappable_tool_paths() -> tuple[tuple[str, str], ...]:
    """ラッパーで呼び直せるコマンドと、シェルから見たその位置。"""
    return tuple(_resolve_tools(tuple(dict.fromkeys(WRAPPABLE_TOOLS))).items())


@functools.lru_cache(maxsize=None)
def to_posix(path: str) -> str:
    """Python から見たパスを、フックを走らせるシェルから見た表記へ移す。"""
    if not IS_WINDOWS:
        return path
    return _bash_out('cygpath -u -- "$1"', path)


@functools.lru_cache(maxsize=None)
def to_native(value: str) -> Path:
    """シェル表記・Windows 表記のいずれからでも Python が扱えるパスへ戻す。"""
    if not IS_WINDOWS:
        return Path(value)
    return Path(_bash_out('cygpath -w -- "$1"', value))


# フックが file_path と基点に適用する解決手順と同じもの。テストが与えた表記が
# 狙った差分（基点が空文字に化けたときだけ一致する形・ドライブのカレント位置を
# 経由してプロジェクト内へ読み替えられる形・cwd 基準でプロジェクト内に落ちる形）を
# 実際に持つことを、入力側の前提条件として確かめるために使う。
# 表記は引数ではなく stdin で渡す。Windows から MSYS の bash を起動すると、
# 引数に含まれる円記号がコマンドラインの再解釈で落ちる（\\?\C:\... が ?C:... になる）。
_NORMAL_FORM_SCRIPT = (
    'IFS= read -r p || exit 1\n'
    'if command -v cygpath >/dev/null 2>&1; then\n'
    '  p=$(cygpath -u -- "$p") || exit 1\n'
    '  p=$(realpath -m -- "$p") || exit 1\n'
    '  p=$(cygpath -w -- "$p") || exit 1\n'
    '  p=$(cygpath -u -- "$p") || exit 1\n'
    'fi\n'
    'realpath -m -- "$p"\n'
)


def shell_normal_form(spelling: str | Path, cwd: str | Path) -> str:
    """その表記が指す場所を、フックを走らせるシェルと同じ手順で解決する。"""
    return _bash_out(_NORMAL_FORM_SCRIPT, cwd=cwd, stdin=f"{spelling}\n")


def abort_if_unsafe_temp_root(root: Path) -> None:
    """隔離ツリーが実データと重なりうる場合は 1 ケースではなく実行全体を止める。"""
    root = root.resolve()
    temp_base = Path(tempfile.gettempdir()).resolve()
    real_claude = REAL_HOME / ".claude"
    problems = []
    if root == temp_base or not root.is_relative_to(temp_base):
        problems.append(f"{root} is not inside {temp_base}")
    for guarded in (REPO_ROOT.resolve(), real_claude):
        if root.is_relative_to(guarded) or guarded.is_relative_to(root):
            problems.append(f"{root} overlaps {guarded}")
    # Windows では一時ディレクトリがホーム配下にあるため、ホームとの重なりは
    # 「ホームそのもの」と「ホームを内側に含む」場合だけを危険とみなす。
    if root == REAL_HOME or REAL_HOME.is_relative_to(root):
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


@unittest.skipUnless(SHELL_READY, SHELL_SKIP_REASON)
class HookPreBoundaryTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hook-pre-boundary-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        abort_if_unsafe_temp_root(self.root)
        self.root_posix = to_posix(str(self.root))

        # プロジェクトルート・スクラッチパッドが自分の位置ではなく渡された環境から
        # 決まることを確かめるため、本体はリポジトリとは無関係な場所に複製して実行する。
        # フックの位置から決まる許可範囲（隣の ../skills）は TestRepositorySkills で扱う。
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

        # Claude Code のスラグはプロジェクトルートのパスから作られるため、
        # 隔離ツリーのプロジェクトルートから同じ形で導く。
        self.slug = self.posix(self.project).replace("/", "-")

        self.skills = self.home / ".claude" / "skills"
        self.memory = self.home / ".claude" / "projects" / self.slug / "memory"

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

        self.scratchpad = self.scratchpad_for(SESSION_ID)
        self.scratchpad.mkdir(parents=True)

    # -- path notation -----------------------------------------------------

    def posix(self, path: str | Path) -> str:
        """隔離ツリー内のパスを、フックが見るシェル表記へ移す。"""
        path = Path(path)
        if path == self.root:
            return self.root_posix
        if path.is_relative_to(self.root):
            return f"{self.root_posix}/{path.relative_to(self.root).as_posix()}"
        return to_posix(str(path))

    def posix_drive_form(self, path: str | Path) -> str:
        """ドライブ文字をそのまま先頭要素にした POSIX 絶対パス（/c/Users/...）。"""
        text = str(Path(path))
        drive, rest = text[0], text[2:]
        return "/" + drive.lower() + rest.replace("\\", "/")

    def drive_relative_form(self, path: str | Path) -> str:
        """ドライブ文字の直後の区切りを落とした表記（C:Users\\... の形）。"""
        native = str(Path(path))
        self.assertRegex(native, r"^[A-Za-z]:[\\/]")
        return native[:2] + native[3:]

    @staticmethod
    def mixed_separators(native: str) -> str:
        head, *tail = native.split("\\")
        out = head
        for index, part in enumerate(tail):
            out += ("/" if index % 2 == 0 else "\\") + part
        return out

    def notations(self, path: str | Path) -> dict[str, str]:
        """同じ場所を指す表記の集合。POSIX 環境では POSIX 表記しか存在しない。"""
        forms = {"posix": self.posix(path)}
        if IS_WINDOWS:
            native = str(Path(path))
            forms["posix drive form"] = self.posix_drive_form(path)
            forms["backslash"] = native
            forms["forward slash"] = native.replace("\\", "/")
            forms["mixed separators"] = self.mixed_separators(native)
        return forms

    # -- fixtures ----------------------------------------------------------

    def scratchpad_for(self, session_id: str, *, uid: int | None = None) -> Path:
        base = self.tmpdir / f"claude-{CURRENT_UID if uid is None else uid}"
        return base / self.slug / session_id / "scratchpad"

    def make_stub(self, directory: Path, name: str, body: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stub = directory / name
        stub.write_text(body, encoding="utf-8", newline="\n")
        os.chmod(stub, 0o755)
        return stub

    def make_wrapper_dir(self, directory: Path, *, omit: Sequence[str] = ()) -> str:
        """実在するコマンドだけを絶対パスで呼び直すラッパーを並べた PATH を作る。

        cygpath だけを欠いた PATH を作るには、cygpath と同じ /usr/bin にある他の
        コマンドを別の場所から見せる必要がある。
        """
        for name, location in wrappable_tool_paths():
            if name in omit:
                continue
            self.make_stub(directory, name, f"#!/bin/sh\nexec '{location}' \"$@\"\n")
        return self.posix(directory)

    def try_symlink(self, link: Path, target: Path) -> bool:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            return False
        return True

    # -- environment -------------------------------------------------------

    def hook_env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.posix(self.bin_dir)}:{MINIMAL_PATH}",
            "TMPDIR": str(self.tmpdir),
            # CLAUDE_PROJECT_DIR は MSYS の環境変数変換の対象外なので、
            # シェルが見る表記をテスト側で明示する。
            "CLAUDE_PROJECT_DIR": self.posix(self.project),
        }
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def abort_if_environment_escapes_tree(
        self, env: dict[str, str], cwd: Path, root_vars: Sequence[str] = ()
    ) -> None:
        """隔離ツリーの外を基準にしたままフックを起動しない。

        root_vars に挙げた変数だけは、ファイルシステムのルート（ドライブのルート）を
        指すことを許す。ルートを基点にしたときの判定を確かめるためで、ルートは
        実在のホームやリポジトリと一致しえず、フックは読み出ししか行わない。
        """
        problems = []
        for name in ("HOME", "TMPDIR", "CLAUDE_PROJECT_DIR"):
            value = env.get(name)
            if not value:
                continue
            try:
                native = to_native(value)
            except RuntimeError:
                problems.append(f"{name}={value} cannot be resolved")
                continue
            if name in root_vars:
                resolved = native.resolve()
                if not resolved.anchor or resolved != Path(resolved.anchor):
                    problems.append(f"{name}={value} is not a filesystem root")
                continue
            if not native.resolve().is_relative_to(self.root):
                problems.append(f"{name}={value}")
        if not Path(cwd).resolve().is_relative_to(self.root):
            problems.append(f"cwd={cwd}")
        home = env.get("HOME")
        if home and to_native(home).resolve() == REAL_HOME:
            problems.append("HOME is the real home directory")
        if problems:
            sys.stderr.write("aborting: environment escapes the test tree: " + "; ".join(problems))
            sys.stderr.write("\n")
            sys.stderr.flush()
            os._exit(3)

    @contextmanager
    def real_environment_unchanged(self) -> Iterator[None]:
        self.assertNotEqual(REAL_DATA_PATHS, (), "no real data path could be snapshotted")
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
            "cwd": self.posix(self.project),
        }
        if not omit_session_id:
            data["session_id"] = session_id
        if not omit_tool_input:
            tool_input: dict[str, object] = {}
            if file_path is not None:
                # Path はシェル表記へ移し、str はそのままの表記で渡す。
                tool_input["file_path"] = (
                    self.posix(file_path) if isinstance(file_path, Path) else str(file_path)
                )
            data["tool_input"] = tool_input
        return json.dumps(data, ensure_ascii=False)

    def run_hook(
        self,
        *,
        stdin: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        empty_vars: Sequence[str] = (),
        root_vars: Sequence[str] = (),
    ) -> HookRun:
        if env is None:
            env = self.hook_env()
        if cwd is None:
            cwd = self.project
        for name in empty_vars:
            self.assertIn(name, EMPTIABLE_VARIABLES, f"{name} must not be emptied")
        for name in root_vars:
            self.assertIn(name, ROOTABLE_VARIABLES, f"{name} must not point at a root")
        self.abort_if_environment_escapes_tree(env, cwd, root_vars)
        self.assertTrue(self.hook.is_file(), f"hook script is missing: {self.hook}")

        if empty_vars:
            # Windows の環境ブロックでは空文字が素通りしないため、空にする操作は
            # シェルの中で行う。
            prologue = "; ".join(f"{name}=''" for name in empty_vars)
            argv = [
                BASH_PATH,
                "-c",
                f'{prologue}\nexec "$1" "$2"',
                "harness",
                BASH_POSIX,
                self.posix(self.hook),
            ]
        else:
            argv = [BASH_PATH, str(self.hook)]

        popen_kwargs: dict[str, object] = {}
        if not IS_WINDOWS:
            popen_kwargs["start_new_session"] = True

        with self.real_environment_unchanged():
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(cwd),
                **popen_kwargs,  # type: ignore[arg-type]
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
        empty_vars: Sequence[str] = (),
        root_vars: Sequence[str] = (),
        **payload_options: object,
    ) -> HookRun:
        stdin = self.payload(file_path, **payload_options)  # type: ignore[arg-type]
        return self.run_hook(
            stdin=stdin, env=env, cwd=cwd, empty_vars=empty_vars, root_vars=root_vars
        )

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

    def assert_stopped_with_exit_2(self, run: HookRun, needles: tuple[str, ...]) -> None:
        """判定を出せないことを exit 2 と stderr の理由で伝えたこと。"""
        self.assertEqual(run.returncode, 2, f"stdout: {run.stdout!r} stderr: {run.stderr!r}")
        self.assertNotEqual(run.stderr.strip(), "", "the reason must be written to stderr")
        for needle in needles:
            self.assertIn(needle, run.stderr)
        self.assertNotIn('"allow"', run.stdout)

    def assert_refused(self, run: HookRun) -> None:
        """exit 2 で止めるか、拒否の JSON を出すかのどちらか。許可（無出力の exit 0）ではない。"""
        if run.returncode == 2:
            self.assertNotEqual(run.stderr.strip(), "", "the reason must be written to stderr")
            self.assertNotIn('"allow"', run.stdout)
        else:
            self.assert_denied(run)

    def assert_decision(self, run: HookRun, *, allowed: bool) -> None:
        if allowed:
            self.assert_allowed(run)
        else:
            self.assert_denied(run)

    def assert_path_allowed(self, file_path: str | Path, **kwargs: object) -> None:
        self.assert_allowed(self.check(file_path, **kwargs))  # type: ignore[arg-type]

    def assert_path_denied(self, file_path: str | Path, **kwargs: object) -> str:
        return self.assert_denied(self.check(file_path, **kwargs))  # type: ignore[arg-type]

    def assert_path_denied_as_unsupported(self, file_path: str | Path, **kwargs: object) -> None:
        """表記そのものを理由に拒否したこと。

        読み替え先が許可範囲の外だったから拒否した、という結果とは区別する。
        表記で拒否する判定はどの許可範囲よりも先に効くため、読み替え先に依存しない。
        """
        reason = self.assert_path_denied(file_path, **kwargs)
        self.assertIn(UNSUPPORTED_NOTATION_REASON, reason)

    # -- simulated platforms -----------------------------------------------

    def posix_shell_env(self) -> dict[str, str]:
        """uname をスタブして POSIX のシェルを模擬した環境を組み立てる。

        スタブが効かないとフックは MSYS 側の分岐に入り、POSIX 向けのケースが
        「UID 成分の無い基点が非空だったので一致しなかっただけ」に化ける。
        スタブが実際に効くことを、同じ PATH でシェルに問い合わせて確かめる。
        """
        stub_dir = self.root / "posix-stub"
        self.make_stub(stub_dir, "uname", "#!/bin/sh\nprintf 'Linux\\n'\n")
        path = f"{self.posix(stub_dir)}:{MINIMAL_PATH}"
        self.assertEqual(
            _bash_out("export PATH=$1; uname -s", path, cwd=self.root),
            "Linux",
            "the uname stub is not in effect; the case would not reach the POSIX branch",
        )
        return self.hook_env(PATH=path)


class TestHarnessCanary(HookPreBoundaryTestBase):
    """ハーネスが allow と deny の両方を観測できていることを先に確かめる。

    PATH の欠落などでフックが常に deny を返す状態になると、deny 側のアサーション
    だけが並んだスイートは全件成功したように見える。
    """

    def test_the_harness_observes_both_an_allow_and_a_deny(self):
        # Arrange
        env = self.hook_env()

        # Act
        allowed = self.check(self.project / "docs" / "note.md", env=env, cwd=self.elsewhere)
        denied = self.check(self.outside / "note.md", env=env, cwd=self.elsewhere)

        # Assert
        self.assert_allowed(allowed)
        self.assert_denied(denied)

    def test_the_harness_reaches_the_hook_under_test(self):
        # Arrange: 差し替え機構が効いていることを、対象だけを壊した複製で確かめる。
        canary = self.root / "copy" / "canary.sh"
        canary.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8", newline="\n")
        os.chmod(canary, 0o755)
        self.hook = canary

        # Act
        run = self.run_hook(stdin=self.payload(self.project / "docs" / "note.md"))

        # Assert
        self.assertEqual(run.returncode, 7, f"stdout={run.stdout!r} stderr={run.stderr!r}")


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
                {"CLAUDE_PROJECT_DIR": f"{self.posix(self.project)}/"},
            ),
            ("a file under the cwd", self.elsewhere / "note.md", False, {}),
            (
                "an outside file while the root does not exist",
                self.home / "other" / "note.md",
                False,
                {"CLAUDE_PROJECT_DIR": self.posix(self.missing_project)},
            ),
            (
                "a cwd file while the root does not exist",
                self.elsewhere / "note.md",
                False,
                {"CLAUDE_PROJECT_DIR": self.posix(self.missing_project)},
            ),
        ]

        for label, target, allowed, overrides in cases:
            with self.subTest(case=label):
                # Act
                run = self.check(target, env=self.hook_env(**overrides), cwd=self.elsewhere)

                # Assert
                self.assert_decision(run, allowed=allowed)

    def test_falls_back_to_the_cwd_when_claude_project_dir_is_absent(self):
        for label, value in (("unset", None), ("empty", "")):
            with self.subTest(claude_project_dir=label):
                # Arrange
                env = self.hook_env(CLAUDE_PROJECT_DIR=value)
                empty_vars = ("CLAUDE_PROJECT_DIR",) if value == "" else ()

                # Act / Assert
                self.assert_allowed(
                    self.check(
                        self.elsewhere / "note.md",
                        env=env,
                        cwd=self.elsewhere,
                        empty_vars=empty_vars,
                    )
                )
                self.assert_denied(
                    self.check(
                        self.project / "docs" / "note.md",
                        env=env,
                        cwd=self.elsewhere,
                        empty_vars=empty_vars,
                    )
                )


class TestScratchpad(HookPreBoundaryTestBase):
    """スクラッチパッドは TMPDIR・実行ユーザーの UID・session_id の 3 点で決まる。"""

    def test_allows_scratchpad_paths_of_the_current_session(self):
        # Arrange: TMPDIR が無い場合のベースはシェルから見た /tmp。実在しない位置を使う。
        fallback = f"{SHELL_TMP}/claude-{CURRENT_UID}/-fallback-probe/{SESSION_ID}/scratchpad"
        self.assertFalse(
            to_native(fallback).exists(),
            f"the fallback probe must not touch existing data: {fallback}",
        )
        cases: list[tuple[str, str | Path, dict[str, str | None], tuple[str, ...]]] = [
            ("a file in the scratchpad", self.scratchpad / "note.md", {}, ()),
            ("a nested file", self.scratchpad / "nested" / "deeper" / "note.md", {}, ()),
            (
                "a path that normalizes back inside",
                f"{self.posix(self.scratchpad)}/nested/../n.md",
                {},
                (),
            ),
            ("tmpdir unset", f"{fallback}/note.md", {"TMPDIR": None}, ()),
            ("tmpdir empty", f"{fallback}/note.md", {}, ("TMPDIR",)),
        ]

        for label, target, overrides, empty_vars in cases:
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_allowed(
                    target, env=self.hook_env(**overrides), empty_vars=empty_vars
                )

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
        foreign_uid = CURRENT_UID + 1
        self.assertNotEqual(foreign_uid, CURRENT_UID)
        base = self.tmpdir / f"claude-{CURRENT_UID}"
        cases: dict[str, str | Path] = {
            "another uid": self.scratchpad_for(SESSION_ID, uid=foreign_uid) / "note.md",
            "directly under tmpdir": self.tmpdir / "note.md",
            "directly under the claude base": base / "note.md",
            "only the slug level": base / self.slug / "note.md",
            # スラグの位置に session_id が来ても、要素の深さが足りなければ通さない。
            "the session id at the slug level": base / SESSION_ID,
            "another temp base": f"{SHELL_VAR_TMP}/claude-{CURRENT_UID}/{SESSION_ID}/n.md",
        }
        for label, target in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(target)

    def test_denies_the_session_directory_itself(self):
        """許可されるのはセッションディレクトリの中身で、ディレクトリそのものではない。"""
        # Arrange
        session_dir = self.tmpdir / f"claude-{CURRENT_UID}" / self.slug / SESSION_ID
        cases = {
            "the session directory": self.posix(session_dir),
            "with a trailing slash": f"{self.posix(session_dir)}/",
        }

        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(spelling)

    def test_denies_a_scratchpad_path_that_leaves_through_a_symlink(self):
        # Arrange
        escape = self.scratchpad / "escape"
        if not self.try_symlink(escape, self.outside):
            self.skipTest("this environment does not allow creating symlinks")
        self.assertTrue(escape.is_symlink())

        # Act / Assert
        self.assert_path_denied(escape / "note.md")

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


class TestScratchpadBaseWithoutUid(HookPreBoundaryTestBase):
    """UID 成分を持たない基点は、それが安全な環境でだけ通す。

    Windows の一時ディレクトリはユーザーごとに分かれているため Claude Code は
    `<tmp>/claude/<スラグ>/<session_id>/` を使う。POSIX の /tmp は共有なので
    同じ形を通すと他ユーザーの領域へ抜けられる。
    """

    def base_without_uid(self, session_id: str = SESSION_ID) -> Path:
        return self.tmpdir / "claude" / self.slug / session_id / "scratchpad"

    @unittest.skipUnless(SHELL_IS_MSYS, "the shell is not MSYS/Cygwin")
    def test_allows_the_base_without_a_uid_on_msys(self):
        # Act / Assert
        self.assert_path_allowed(self.base_without_uid() / "note.md")
        self.assert_path_allowed(self.base_without_uid() / "nested" / "note.md")

    @unittest.skipUnless(SHELL_IS_MSYS, "the shell is not MSYS/Cygwin")
    def test_still_requires_the_session_id_on_the_base_without_a_uid(self):
        # Act / Assert
        self.assert_path_denied(self.base_without_uid(OTHER_SESSION_ID) / "note.md")
        self.assert_path_denied(self.base_without_uid() / "note.md", session_id="")

    @unittest.skipUnless(SHELL_IS_MSYS, "the shell is not MSYS/Cygwin")
    def test_denies_the_session_directory_itself_on_the_base_without_a_uid(self):
        """許可されるのはセッションディレクトリの中身で、ディレクトリそのものではない。"""
        # Arrange
        session_dir = self.base_without_uid().parent
        self.assertEqual(session_dir.name, SESSION_ID)
        cases = {
            "the session directory": self.posix(session_dir),
            "with a trailing slash": f"{self.posix(session_dir)}/",
        }

        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(spelling)

    def test_denies_the_base_without_a_uid_on_posix(self):
        # Arrange
        env = self.posix_shell_env()

        # Act / Assert: スタブが効いていることを allow 側で先に確かめる。
        self.assert_path_allowed(self.scratchpad / "note.md", env=env)
        self.assert_path_denied(self.base_without_uid() / "note.md", env=env)

    def test_denies_a_path_that_only_repeats_the_session_id_on_posix(self):
        """POSIX では UID 成分を持たない基点そのものが存在しない。

        存在しない基点を空文字のまま前方一致に使うと、照合パターンが `/*/*/*` へ
        化ける。どの基点の下でもないのに第 2 要素が session_id と一致するだけの
        パスが、この化けたパターンを満たす。
        """
        # Arrange
        env = self.posix_shell_env()
        cases = {
            "a system directory": f"/etc/{SESSION_ID}/passwd",
            "a sibling of the temp root": f"{SHELL_TMP}/{SESSION_ID}/note.md",
        }

        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Arrange: 基点が空文字に化けたときだけ一致する形であることを確かめる。
                resolved = shell_normal_form(spelling, self.project)
                segments = resolved.split("/")
                self.assertEqual(segments[0], "", f"{spelling} resolves to {resolved}")
                self.assertGreaterEqual(len(segments), 4, f"{spelling} resolves to {resolved}")
                self.assertEqual(segments[2], SESSION_ID, f"{spelling} resolves to {resolved}")

                # Act / Assert
                self.assert_path_denied(spelling, env=env)


class TestExistingExceptions(HookPreBoundaryTestBase):
    """スキルと auto memory への書き込みは従来どおり通し、その隣接は通さない。"""

    def test_keeps_the_skills_and_memory_exceptions(self):
        claude = self.home / ".claude"
        cases: list[tuple[str, Path, bool]] = [
            ("the skills directory itself", self.skills, True),
            ("a skill file", self.skills / "tdd" / "SKILL.md", True),
            ("a memory file", self.memory / "MEMORY.md", True),
            ("a sibling of memory", claude / "projects" / self.slug / "notes" / "note.md", False),
            ("without a project segment", claude / "projects" / "memory" / "note.md", False),
            # <名前> は 1 要素に限る。
            (
                "two segments in place of the project name",
                claude / "projects" / "a" / "b" / "memory" / "x.md",
                False,
            ),
            ("the settings file", claude / "settings.json", False),
        ]
        for label, target, allowed in cases:
            with self.subTest(case=label):
                # Act
                run = self.check(target, cwd=self.elsewhere)

                # Assert
                self.assert_decision(run, allowed=allowed)


class TestAllowedBasesTerminate(HookPreBoundaryTestBase):
    """許可の基点は名前の切れ目で終端する。前方一致が続きの文字へ伸びてはならない。

    プロジェクトルート・skills・memory はいずれも「基点そのもの」か「基点の直下」
    としてしか通らない。区切りの `/` と先頭のアンカーという終端条件が落ちると、
    `project-sibling` や `skills.md` のような隣の場所、さらにはパスの途中にホーム
    相当の文字列を含むだけの無関係な場所が、そのまま許可側へ化ける。
    """

    def normal_form(self, spelling: str) -> str:
        """フックと同じ手順で解決した正規形。ケースの前提条件の確認に使う。"""
        return shell_normal_form(spelling, self.elsewhere)

    def assert_is_prefix_sibling(self, spelling: str, base: str) -> None:
        """その表記が『基点の文字列で始まるが基点の内側ではない』ことを確かめる。

        正規化の段階で末尾が畳まれるなどして基点の内側へ落ちてしまうと、ケースは
        黙って別の判定を測る。判定を見る前に、狙った形を保っていることを押さえる。
        """
        resolved = self.normal_form(spelling)
        base_resolved = self.normal_form(base)
        self.assertTrue(
            resolved.startswith(base_resolved),
            f"{spelling} resolves to {resolved}, which does not share the prefix {base_resolved}",
        )
        self.assertNotEqual(resolved, base_resolved, f"{spelling} resolves to the base itself")
        self.assertFalse(
            resolved.startswith(f"{base_resolved}/"),
            f"{spelling} resolves to {resolved}, which is inside the base {base_resolved}",
        )

    def test_denies_a_sibling_that_only_extends_the_project_root(self):
        # Arrange: 基点が開いたままであることを同じ組で押さえ、deny 一色の空振りを避ける。
        project = self.posix(self.project)
        cases: list[tuple[str, str, bool]] = [
            ("a file inside the project root", f"{project}/docs/note.md", True),
            ("a directory whose name extends the root", f"{project}-sibling/note.md", False),
            ("a file whose name extends the root", f"{project}.md", False),
        ]

        for label, spelling, allowed in cases:
            with self.subTest(case=label, path=spelling):
                if not allowed:
                    self.assert_is_prefix_sibling(spelling, project)

                # Act
                run = self.check(spelling, cwd=self.elsewhere)

                # Assert
                self.assert_decision(run, allowed=allowed)

    def test_denies_a_sibling_that_only_extends_the_skills_directory(self):
        # Arrange
        skills = self.posix(self.skills)
        cases: list[tuple[str, str, bool]] = [
            ("a file inside the skills directory", f"{skills}/tdd/SKILL.md", True),
            ("a directory whose name extends skills", f"{skills}-sibling/SKILL.md", False),
            ("a file whose name extends skills", f"{skills}.md", False),
        ]

        for label, spelling, allowed in cases:
            with self.subTest(case=label, path=spelling):
                if not allowed:
                    self.assert_is_prefix_sibling(spelling, skills)

                # Act
                run = self.check(spelling, cwd=self.elsewhere)

                # Assert
                self.assert_decision(run, allowed=allowed)

    def test_denies_a_memory_shaped_path_that_does_not_start_at_the_home(self):
        # Arrange: ホーム配下と同じ並びを、別の場所の途中に埋め込んだ表記。
        fragment = f"{self.posix(self.home)}/.claude/projects/{self.slug}/memory"
        embedded = f"{self.posix(self.elsewhere)}{fragment}/m.md"

        # Arrange: 埋め込んだ並びが正規化後も『途中に現れるが先頭ではない』ことを押さえる。
        resolved = self.normal_form(embedded)
        fragment_resolved = self.normal_form(fragment)
        self.assertIn(f"{fragment_resolved}/", resolved, f"{embedded} resolves to {resolved}")
        self.assertFalse(
            resolved.startswith(fragment_resolved),
            f"{embedded} resolves to {resolved}, which starts at the home itself",
        )

        cases: list[tuple[str, str, bool]] = [
            ("a memory file under the home", f"{fragment_resolved}/m.md", True),
            ("the same shape embedded in another path", embedded, False),
        ]

        for label, spelling, allowed in cases:
            with self.subTest(case=label, path=spelling):
                # Act
                run = self.check(spelling, cwd=self.elsewhere)

                # Assert
                self.assert_decision(run, allowed=allowed)


class TestPathNotation(HookPreBoundaryTestBase):
    """同じ場所を指す限り、パスの表記が変わっても判定は変わらない。

    ツールから渡る file_path は Windows 表記のことがあり、環境変数は POSIX 表記の
    ことがある。両者を同じ正規形へ寄せないと、前方一致の判定が常に外れる。
    """

    def target_cases(self) -> list[tuple[str, Path, bool]]:
        return [
            ("a file in the project", self.project / "docs" / "note.md", True),
            ("the project root itself", self.project, True),
            ("a skill file", self.skills / "tdd" / "SKILL.md", True),
            ("a memory file", self.memory / "MEMORY.md", True),
            ("a scratchpad file", self.scratchpad / "note.md", True),
            ("a file under the home", self.home / "other" / "note.md", False),
            ("a file outside every base", self.outside / "note.md", False),
        ]

    def test_the_decision_does_not_depend_on_the_file_path_notation(self):
        for label, target, allowed in self.target_cases():
            for notation, spelling in self.notations(target).items():
                with self.subTest(case=label, notation=notation):
                    # Act
                    run = self.check(spelling, cwd=self.elsewhere)

                    # Assert
                    self.assert_decision(run, allowed=allowed)

    def test_the_decision_does_not_depend_on_the_project_root_notation(self):
        root_forms = self.notations(self.project)
        file_forms = ("posix", "backslash", "forward slash", "mixed separators")
        targets = [
            ("a file in the project", self.project / "docs" / "note.md", True),
            ("a file outside the project", self.outside / "note.md", False),
        ]
        for root_notation, root_spelling in root_forms.items():
            env = self.hook_env(CLAUDE_PROJECT_DIR=root_spelling)
            for label, target, allowed in targets:
                notations = self.notations(target)
                for notation in file_forms:
                    if notation not in notations:
                        continue
                    with self.subTest(root=root_notation, case=label, notation=notation):
                        # Act
                        run = self.check(notations[notation], env=env, cwd=self.elsewhere)

                        # Assert
                        self.assert_decision(run, allowed=allowed)


class TestUnsupportedNotations(HookPreBoundaryTestBase):
    """想定しない表記は、どこを指していても通さない。"""

    @unittest.skipUnless(IS_WINDOWS, "drive-relative paths only exist on Windows")
    def test_denies_a_drive_relative_path(self):
        """ドライブ相対はドライブごとのカレント位置を経由して読み替えられる。

        読み替えた先がプロジェクト内であっても、その読み替えを根拠には通さない。
        """
        # Arrange
        target = self.project / "docs" / "note.md"
        reaches_the_project = self.drive_relative_form(target)
        drive = str(self.project)[0]
        cases = {
            "reads back into the project": reaches_the_project,
            "a directory on the drive": f"{drive}:docs/note.md",
            "a bare name on the drive": f"{drive}:note.md",
        }

        for cwd_label, cwd in (
            ("cwd inside the project", self.project),
            ("cwd elsewhere", self.elsewhere),
        ):
            # Arrange: 読み替え先がプロジェクト内に入る形であることを確かめる。
            self.assertEqual(
                shell_normal_form(reaches_the_project, cwd),
                shell_normal_form(self.posix(target), cwd),
                "the drive-relative spelling no longer reads back into the project",
            )
            for label, spelling in cases.items():
                with self.subTest(cwd=cwd_label, case=label):
                    # Act / Assert
                    self.assert_path_denied(spelling, cwd=cwd)

    def test_denies_a_relative_path(self):
        """相対パスはフックを起動したプロセスの cwd を基準に解決される。

        Write ツールが解決する位置と一致する保証がないため、cwd 基準の解決先が
        プロジェクト内であっても通さない。
        """
        # Arrange
        cwd = self.project
        project_form = shell_normal_form(self.posix(self.project), cwd)
        cases = {
            "a bare name": "note.md",
            "a subdirectory": "relative/note.md",
            "an explicit dot": "./note.md",
            "a path that climbs back": "docs/../note.md",
        }

        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Arrange: cwd 基準ではプロジェクト内へ解決される形であることを確かめる。
                resolved = shell_normal_form(spelling, cwd)
                self.assertTrue(
                    resolved.startswith(f"{project_form}/"),
                    f"{spelling} does not resolve inside the project: {resolved}",
                )

                # Act / Assert
                self.assert_path_denied(spelling, cwd=cwd)

    # 先頭 2 文字に置かれうる区切りの組。MSYS/Cygwin では円記号も区切りとして読まれるため、
    # 区切り文字の集合 {/, 円記号} から 2 つ選ぶ組すべてが UNC・デバイス表記になる。
    LEADING_SEPARATOR_PAIRS = {
        "two slashes": "//",
        "two backslashes": chr(92) * 2,
        "a slash then a backslash": "/" + chr(92),
        "a backslash then a slash": chr(92) + "/",
    }

    def test_denies_a_unc_or_extended_length_path(self):
        """先頭に区切りが 2 つ並ぶ表記はマウント表を経由して読み替えられる。

        区切りの組み合わせ（/ と円記号）は問わない。読み替えた先がプロジェクト内で
        あっても、その読み替えを根拠には通さず、表記そのものを理由に拒否する。
        """
        # Arrange
        backslash = chr(92)
        cases: dict[str, str] = {}
        for pair_label, pair in self.LEADING_SEPARATOR_PAIRS.items():
            cases[f"a unc path with {pair_label}"] = (
                pair + "server" + backslash + "share" + backslash + "note.md"
            )
        if IS_WINDOWS:
            file_in_project = self.project / "docs" / "note.md"
            native = str(file_in_project)
            reads_back: dict[str, str] = {}
            for pair_label, pair in self.LEADING_SEPARATOR_PAIRS.items():
                reads_back[f"an extended-length prefix with {pair_label}"] = (
                    pair + "?" + backslash + native
                )
                cases[f"a device prefix with {pair_label}"] = pair + "." + backslash + native
            reads_back["an extended-length prefix with forward slashes only"] = (
                "//?/" + native.replace(backslash, "/")
            )
            for label, spelling in reads_back.items():
                # Arrange: 読み替え先がプロジェクト内に入る形であることを確かめる。
                self.assertEqual(
                    shell_normal_form(spelling, self.elsewhere),
                    shell_normal_form(self.posix(file_in_project), self.elsewhere),
                    f"{label} no longer reads back into the project",
                )
            cases.update(reads_back)

        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied_as_unsupported(spelling, cwd=self.elsewhere)

    def test_does_not_hand_the_host_of_a_unc_path_to_path_resolution(self):
        """UNC 表記のホスト名は、パスを解決するコマンドへ渡らない。

        cygpath・realpath に UNC を渡すとホストへの接続を試み、存在しないホストでは
        名前解決のタイムアウトまで書き込みが止まる。表記で拒否する判定が正規化より
        先に効けば、ホスト名はどのコマンドにも届かない。
        """
        # Arrange: cygpath と realpath を、受け取った引数を記録してから本物を呼ぶ形で包む。
        backslash = chr(92)
        host = "no-such-host.invalid"
        bin_dir = self.root / "recording-bin"
        log = self.root / "resolution-args.log"
        wrapped = ["realpath"] + (["cygpath"] if SHELL_IS_MSYS else [])
        for name in wrapped:
            self.make_stub(
                bin_dir,
                name,
                f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{self.posix(log)}'\n"
                f"exec '{TOOL_PATHS[name]}' \"$@\"\n",
            )
        env = self.hook_env(PATH=f"{self.posix(bin_dir)}:{MINIMAL_PATH}")

        # Arrange: 記録が実際に取れていることを、正規化を通る書き込み先で先に確かめる。
        self.assert_path_allowed(self.project / "docs" / "note.md", env=env, cwd=self.elsewhere)
        self.assertTrue(log.is_file(), "the recording wrappers were never invoked")
        self.assertIn("note.md", log.read_text(encoding="utf-8"))

        for pair_label, pair in self.LEADING_SEPARATOR_PAIRS.items():
            spelling = pair + host + backslash + "share" + backslash + "note.md"
            with self.subTest(case=pair_label):
                offset = log.stat().st_size

                # Act
                run = self.check(spelling, env=env, cwd=self.elsewhere)

                # Assert
                with log.open(encoding="utf-8") as stream:
                    stream.seek(offset)
                    recorded = stream.read()
                self.assertNotIn(host, recorded, f"{spelling!r} reached path resolution")
                self.assertIn(UNSUPPORTED_NOTATION_REASON, self.assert_denied(run))

    def test_denies_a_drive_absolute_path_outside_msys(self):
        """ドライブ絶対パスが絶対パスとして読まれるのは MSYS/Cygwin だけである。

        POSIX のシェルでは `C:/note.md` は cwd 基準の相対パスとして解決される。
        cwd がプロジェクト内でその解決先がプロジェクト内に落ちても、通さない。
        """
        # Arrange
        env = self.posix_shell_env()
        cwd = self.project
        backslash = chr(92)
        cases = {
            "forward slash": "C:/note.md",
            "backslash": "C:" + backslash + "note.md",
            "a lowercase drive letter": "c:/docs/note.md",
        }
        self.assert_path_allowed(self.project / "docs" / "note.md", env=env, cwd=cwd)

        for label, spelling in cases.items():
            with self.subTest(case=label):
                if not SHELL_IS_MSYS:
                    # Arrange: cwd 基準ではプロジェクト内へ解決される形であることを確かめる。
                    project_form = shell_normal_form(self.posix(self.project), cwd)
                    resolved = shell_normal_form(spelling, cwd)
                    self.assertTrue(
                        resolved.startswith(f"{project_form}/"),
                        f"{spelling} does not resolve inside the project: {resolved}",
                    )

                # Act / Assert
                self.assert_path_denied_as_unsupported(spelling, env=env, cwd=cwd)


class TestDegenerateBases(HookPreBoundaryTestBase):
    """基点の正規形が得られないとき、その基点は全許可に化けてはならない。"""

    def test_an_empty_home_does_not_open_the_skills_and_memory_exceptions(self):
        cases = {
            "the memory exception": "/.claude/projects/a-project/memory/note.md",
            "the skills exception": "/.claude/skills/tdd/SKILL.md",
            "the skills directory itself": "/.claude/skills",
            "the filesystem root": "/note.md",
        }
        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(spelling, cwd=self.elsewhere, empty_vars=("HOME",))

    def test_an_empty_tmpdir_does_not_open_the_scratchpad_of_the_isolated_tree(self):
        # Arrange: TMPDIR が空ならスクラッチパッドの基点は /tmp へ移る。
        cases: dict[str, str] = {
            "the tree scratchpad": self.posix(self.scratchpad / "note.md"),
            "a bare claude base at the root": f"/claude-{CURRENT_UID}/{self.slug}/{SESSION_ID}/n.md",
        }
        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(spelling, cwd=self.elsewhere, empty_vars=("TMPDIR",))

    def test_an_unresolvable_project_root_does_not_open_the_filesystem(self):
        # Arrange: 存在しないルートでも、その外側が通ってはならない。
        env = self.hook_env(CLAUDE_PROJECT_DIR=self.posix(self.missing_project))
        for label, spelling in (
            ("the filesystem root", "/note.md"),
            ("a sibling of the missing root", self.posix(self.root / "missing" / "note.md")),
        ):
            with self.subTest(case=label):
                # Act / Assert
                self.assert_path_denied(spelling, env=env, cwd=self.elsewhere)


@unittest.skipUnless(SHELL_IS_MSYS, "drive roots only exist on MSYS/Cygwin")
class TestFilesystemRootBases(HookPreBoundaryTestBase):
    """正規化した基点がファイルシステムのルートであれば、その基点は許可に使われない。

    MSYS/Cygwin ではドライブのルート（/c）が POSIX の / と同じ意味を持つ。
    POSIX では 1 文字の名前の最上位ディレクトリ（/c）はルートではない。
    """

    def setUp(self):
        super().setUp()
        self.drive = str(self.root)[0].lower()
        self.drive_root = f"/{self.drive}"

    def drive_root_spellings(self) -> dict[str, str]:
        """隔離ツリーと同じドライブのルートを指す表記の集合。"""
        letter = self.drive
        backslash = chr(92)
        spellings = {
            "a backslash drive root": f"{letter.upper()}:{backslash}",
            "a forward slash drive root": f"{letter.upper()}:/",
            "a lowercase drive letter": f"{letter}:{backslash}",
            "a posix drive root": f"/{letter}",
            "a posix drive root with a trailing slash": f"/{letter}/",
            "a dot-dot back to the drive root": f"{letter.upper()}:{backslash}Windows{backslash}..",
        }
        for label, spelling in spellings.items():
            self.assertEqual(
                shell_normal_form(spelling, self.elsewhere),
                self.drive_root,
                f"{label} no longer resolves to the drive root",
            )
        return spellings

    def drive_targets(self) -> dict[str, str | Path]:
        """ドライブ上にあり、どの許可範囲にも入らない書き込み先。"""
        return {
            "a file outside every base": self.outside / "note.md",
            "a file at the drive root": f"{self.drive_root}/note.md",
            "a system file": f"{self.drive_root}/Windows/x",
        }

    def test_a_drive_root_project_dir_does_not_allow_writes_on_the_drive(self):
        """MSYS で CLAUDE_PROJECT_DIR がドライブのルートのとき、そのドライブ上の書き込みはプロジェクトとしては許可されない。"""
        for notation, spelling in self.drive_root_spellings().items():
            env = self.hook_env(CLAUDE_PROJECT_DIR=spelling)
            for label, target in self.drive_targets().items():
                with self.subTest(notation=notation, case=label):
                    # Act / Assert
                    self.assert_path_denied(
                        target, env=env, cwd=self.elsewhere, root_vars=("CLAUDE_PROJECT_DIR",)
                    )

    def test_a_drive_root_project_dir_keeps_the_other_exceptions(self):
        """MSYS で CLAUDE_PROJECT_DIR がドライブのルートでも、skills・スクラッチパッド・memory への書き込みは許可される。"""
        targets: dict[str, Path] = {
            "a skill file": self.skills / "tdd" / "SKILL.md",
            "a scratchpad file": self.scratchpad / "note.md",
            "a memory file": self.memory / "MEMORY.md",
        }
        for notation, spelling in self.drive_root_spellings().items():
            env = self.hook_env(CLAUDE_PROJECT_DIR=spelling)
            for label, target in targets.items():
                with self.subTest(notation=notation, case=label):
                    # Act / Assert
                    self.assert_path_allowed(
                        target, env=env, cwd=self.elsewhere, root_vars=("CLAUDE_PROJECT_DIR",)
                    )

    def test_a_drive_root_home_does_not_open_the_drive(self):
        """MSYS で HOME がドライブのルートのとき、HOME を基点にした範囲（skills・memory）でドライブ全体が開かない。"""
        # Arrange: HOME を基点にする memory の範囲も、ルートの基点からは作らない。
        targets: dict[str, str | Path] = {
            **self.drive_targets(),
            "a memory-shaped path under the drive root": (
                f"{self.drive_root}/.claude/projects/{self.slug}/memory/MEMORY.md"
            ),
        }
        for notation, spelling in self.drive_root_spellings().items():
            env = self.hook_env(HOME=spelling)
            for label, target in targets.items():
                with self.subTest(notation=notation, case=label):
                    # Act / Assert
                    self.assert_path_denied(
                        target, env=env, cwd=self.elsewhere, root_vars=("HOME",)
                    )

    def test_a_single_letter_top_directory_is_still_a_base_on_posix(self):
        """POSIX では /c のような 1 文字の最上位ディレクトリはルートではなく、基点として使われる。"""
        # Arrange: uname を Linux に差し替え、POSIX の分岐を通す。
        posix_env = self.posix_shell_env()
        cases: dict[str, tuple[str, str]] = {
            "the project root": ("CLAUDE_PROJECT_DIR", f"{self.drive_root}/note.md"),
            "the memory under the home": (
                "HOME",
                f"{self.drive_root}/.claude/projects/{self.slug}/memory/MEMORY.md",
            ),
        }
        for label, (name, target) in cases.items():
            with self.subTest(base=label):
                env = {**posix_env, name: self.drive_root}

                # Act / Assert
                self.assert_path_allowed(target, env=env, cwd=self.elsewhere, root_vars=(name,))


class TestRequiredToolsAreMandatory(HookPreBoundaryTestBase):
    """MSYS/Cygwin で表記の正規化に必要な cygpath が使えない環境では、判定を通さない。

    cygpath が PATH に無いことは jq・realpath が無いことと同じく依存コマンドの欠落として
    扱い、理由を stderr に出して exit 2 で止める。PreToolUse では exit 1 や 127 は
    呼び出しを止めない。
    """

    @unittest.skipUnless(SHELL_IS_MSYS, "cygpath is only required on MSYS/Cygwin")
    def test_stops_with_exit_2_when_cygpath_is_missing_from_the_path(self):
        # Arrange: cygpath だけを欠いた最小 PATH を組み立てる。
        complete = self.make_wrapper_dir(self.root / "wrap-all")
        without_cygpath = self.make_wrapper_dir(self.root / "wrap-no-cygpath", omit=("cygpath",))
        targets: dict[str, Path] = {
            "a project file": self.project / "docs" / "note.md",
            "a skill file": self.skills / "tdd" / "SKILL.md",
            "a file outside every base": self.outside / "note.md",
        }

        # Act / Assert: まず PATH の組み立て自体が健全であることを確かめる。
        self.assert_path_allowed(targets["a project file"], env=self.hook_env(PATH=complete))
        self.assert_path_denied(
            targets["a file outside every base"], env=self.hook_env(PATH=complete)
        )

        for label, target in targets.items():
            with self.subTest(case=label):
                # Act
                run = self.check(target, env=self.hook_env(PATH=without_cygpath))

                # Assert
                self.assert_stopped_with_exit_2(run, ("cygpath",))

    @unittest.skipUnless(SHELL_IS_MSYS, "cygpath is only required on MSYS/Cygwin")
    def test_refuses_everything_when_cygpath_fails(self):
        # Arrange
        stub_dir = self.root / "broken-cygpath"
        self.make_stub(stub_dir, "cygpath", "#!/bin/sh\nexit 1\n")
        env = self.hook_env(PATH=f"{self.posix(stub_dir)}:{MINIMAL_PATH}")

        for label, target in (
            ("a project file", self.project / "docs" / "note.md"),
            ("a skill file", self.skills / "tdd" / "SKILL.md"),
            ("a scratchpad file", self.scratchpad / "note.md"),
        ):
            with self.subTest(case=label):
                # Act / Assert
                self.assert_refused(self.check(target, env=env))


class TestRepositorySkills(HookPreBoundaryTestBase):
    """フックの実体が属するリポジトリの .claude/skills へは書き込める。

    ~/.claude/skills が skill 単位のリンクを並べた実ディレクトリでも、リンク越しに
    リポジトリ側へ書き込める。それ以外のリポジトリ内と、想定しない表記は通さない。
    """

    def setUp(self):
        super().setUp()
        # リポジトリと同じ相対配置に複製する。フックの隣の ../skills は repo/.claude/skills。
        self.repo_claude = self.root / "repo" / ".claude"
        self.hook = self.repo_claude / "scripts" / HOOK_NAME
        self.hook.parent.mkdir(parents=True)
        shutil.copy2(HOOK_UNDER_TEST, self.hook)

        self.repo_skills = self.repo_claude / "skills"
        (self.repo_skills / "tdd").mkdir(parents=True)
        (self.repo_skills / "tdd" / "SKILL.md").write_text("tdd\n", encoding="utf-8")
        (self.repo_claude / "settings.json").write_text("{}\n", encoding="utf-8")
        (self.repo_claude / "skills-archive").mkdir()

        # deploy 後の ~/.claude/skills: 実ディレクトリに skill 単位のリンクと実行時データ。
        self.assertFalse(self.skills.is_symlink())
        (self.skills / "_tracker").mkdir()
        self.links_available = self.try_symlink(
            self.skills / "tdd", self.repo_skills / "tdd"
        ) and self.try_symlink(self.skills / "evil", self.outside)

    def require_links(self) -> None:
        if not self.links_available:
            self.skipTest("this environment does not allow creating symlinks")

    def check_from_another_project(self, target: str | Path) -> HookRun:
        return self.check(target, cwd=self.elsewhere)

    def test_allows_writes_into_repository_skills_in_every_notation(self):
        cases: dict[str, Path] = {
            "the repository skill file itself": self.repo_skills / "tdd" / "SKILL.md",
            "a new skill in the repository": self.repo_skills / "new-skill" / "SKILL.md",
            "the repository skills directory itself": self.repo_skills,
        }
        for label, target in cases.items():
            for notation, spelling in self.notations(target).items():
                with self.subTest(case=label, notation=notation):
                    # Act / Assert
                    self.assert_allowed(self.check_from_another_project(spelling))

    def test_allows_writes_into_repository_skills_through_a_skill_link(self):
        # Arrange
        self.require_links()
        cases: dict[str, Path] = {
            "a new file through a skill link": self.skills / "tdd" / "feedback" / "x.md",
            "an existing file through a skill link": self.skills / "tdd" / "SKILL.md",
        }
        for label, target in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_allowed(self.check_from_another_project(target))

    def test_allows_writes_into_the_real_skills_directory(self):
        # Act / Assert
        self.assert_allowed(self.check_from_another_project(self.skills / "_tracker" / "x.log"))

    def test_allows_repository_skills_when_the_hook_is_reached_through_a_symlink(self):
        # Arrange: リポジトリへのパスがシンボリックリンクを経由していても、
        # フックの実体の位置から許可範囲を決める。
        alias = self.root / "repo-alias"
        if not self.try_symlink(alias, self.root / "repo"):
            self.skipTest("this environment does not allow creating symlinks")
        self.hook = alias / ".claude" / "scripts" / HOOK_NAME

        # Act / Assert
        self.assert_allowed(
            self.check_from_another_project(self.repo_skills / "tdd" / "feedback" / "x.md")
        )

    def test_allows_repository_skills_when_the_hook_itself_is_a_file_link(self):
        """フック本体がファイル単位のリンクで置かれていても、許可範囲は実体の位置から決まる。

        リンクの置き場所の隣にある ../skills は、実体のリポジトリではないので通さない。
        """
        # Arrange
        link_root = self.root / "file-link"
        (link_root / "scripts").mkdir(parents=True)
        (link_root / "skills").mkdir()
        link = link_root / "scripts" / HOOK_NAME
        if not self.try_symlink(link, self.hook):
            self.skipTest("this environment does not allow creating symlinks")
        self.assertTrue(link.is_symlink())
        self.hook = link

        # Act / Assert
        self.assert_allowed(
            self.check_from_another_project(self.repo_skills / "tdd" / "feedback" / "x.md")
        )
        self.assert_denied(self.check_from_another_project(link_root / "skills" / "x.md"))

    def test_denies_other_paths_in_the_repository(self):
        cases: dict[str, Path] = {
            "a hook script": self.repo_claude / "scripts" / "x.sh",
            "the repository settings": self.repo_claude / "settings.json",
            "a sibling sharing the skills prefix": self.repo_claude / "skills-archive" / "x",
            "a file whose name extends skills": self.repo_claude / "skills.md",
            "a dot-dot escape from repository skills": self.repo_skills / ".." / "settings.json",
            "the repository root": self.root / "repo" / "x",
        }
        for label, target in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_denied(self.check_from_another_project(target))

    def test_denies_escapes_through_skill_links(self):
        # Arrange
        self.require_links()
        cases: dict[str, Path] = {
            "a dot-dot escape through a skill link": (
                self.skills / "tdd" / ".." / ".." / "settings.json"
            ),
            "a skill link pointing outside": self.skills / "evil" / "x",
        }
        for label, target in cases.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_denied(self.check_from_another_project(target))

    def test_denies_unsupported_notations_that_read_back_into_repository_skills(self):
        """表記のホワイトリストは、リポジトリの skills の許可より先に効く。"""
        # Arrange: どの表記も、読み替えた先はリポジトリの skills の中に入る。
        target = self.repo_skills / "tdd" / "SKILL.md"
        scripts_dir = self.repo_claude / "scripts"
        cases: dict[str, tuple[str, Path]] = {
            "a relative path from the hook directory": ("../skills/tdd/SKILL.md", scripts_dir),
        }
        if IS_WINDOWS:
            backslash = chr(92)
            cases["a drive-relative path"] = (self.drive_relative_form(target), scripts_dir)
            for pair_label, pair in TestUnsupportedNotations.LEADING_SEPARATOR_PAIRS.items():
                cases[f"an extended-length prefix with {pair_label}"] = (
                    pair + "?" + backslash + str(target),
                    self.elsewhere,
                )

        for label, (spelling, cwd) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(
                    shell_normal_form(spelling, cwd),
                    shell_normal_form(self.posix(target), cwd),
                    f"{label} no longer reads back into the repository skills",
                )

                # Act / Assert
                self.assert_path_denied_as_unsupported(spelling, cwd=cwd)


@unittest.skipUnless(GNU_REALPATH and JQ_PATH, "jq or a realpath that accepts -m is not available")
class TestDependencyCommands(HookPreBoundaryTestBase):
    """依存コマンド（jq と、-m を使える realpath / grealpath）が期待どおり動かないときは
    許可せず、exit 2 で止める。PreToolUse では exit 2 だけが呼び出しを止め、stderr が理由になる。

    MSYS/Cygwin では同じケースが cygpath を経由する正規化の経路を通る。
    """

    JQ_NEEDLES = ("jq", "PATH")
    REALPATH_NEEDLES = ("coreutils", "brew install coreutils")

    def setUp(self):
        super().setUp()
        self.variant_count = 0
        self.variant_bin: Path | None = None
        self.has_escape_link = self.try_symlink(self.scratchpad / "escape", self.outside)

    # -- fixtures ----------------------------------------------------------

    def stub_marker(self, bin_dir: Path) -> str:
        """スタブが仕込んだ失敗を実際に起こしたときに作る印（シェル表記）。"""
        return self.posix(bin_dir / "triggered")

    def place_jq(self, bin_dir: Path, kind: str) -> None:
        assert JQ_PATH is not None
        mark = f": > '{self.stub_marker(bin_dir)}'"
        if kind == "real":
            body = f"exec '{JQ_PATH}' \"$@\"\n"
        elif kind == "failing":
            body = "exit 1\n"
        elif kind == "fails-on-deny":
            # 判定の途中までは本物として動き、拒否の JSON を作る呼び出しだけ失敗する。
            body = (
                'for a in "$@"; do case "$a" in *permissionDecision*)'
                f" {mark}; exit 1;; esac; done\n"
                f"exec '{JQ_PATH}' \"$@\"\n"
            )
        elif kind == "fails-on-session-id":
            # session_id の取り出しだけ、正しい値を出力したうえで失敗の終了コードを返す。
            # 出力だけを見て終了コードを確かめない実装は、この値を信じてしまう。
            body = (
                'for a in "$@"; do case "$a" in *session_id*)\n'
                f"  {mark}; echo '{SESSION_ID}'; exit 1;;\n"
                "esac; done\n"
                f"exec '{JQ_PATH}' \"$@\"\n"
            )
        elif kind == "absent":
            return
        else:
            raise ValueError(kind)
        self.make_stub(bin_dir, "jq", "#!/bin/sh\n" + body)

    def place_realpath(self, bin_dir: Path, name: str, kind: str) -> None:
        assert GNU_REALPATH is not None
        mark = f": > '{self.stub_marker(bin_dir)}'"
        real = f"exec '{GNU_REALPATH}' \"$@\"\n"
        if kind == "real":
            body = real
        elif kind == "no-m":
            # macOS 標準の realpath の再現: オプションを受け付けず、存在しないパスは失敗する。
            body = (
                'for a in "$@"; do case "$a" in -*)\n'
                '  echo "realpath: illegal option -- ${a#-}" >&2\n'
                '  echo "usage: realpath [-q] [path ...]" >&2\n'
                "  exit 1;;\n"
                "esac; done\n"
                f"exec '{GNU_REALPATH}' -e -- \"$@\"\n"
            )
        elif kind.startswith("fails-on:"):
            # -m 付きで動くが、指定した形のパスの解決だけ失敗する（途中での失敗）。
            pattern = kind.removeprefix("fails-on:")
            body = f'for a in "$@"; do case "$a" in {pattern}) {mark}; exit 1;; esac; done\n' + real
        elif kind.startswith("fails-printing:"):
            # 指定したパスの解決だけ、別のパスを出力したうえで失敗の終了コードを返す。
            # 形式: fails-printing:<出力する値>::<case のパターン>
            printed, pattern = kind.removeprefix("fails-printing:").split("::", 1)
            body = (
                f'for a in "$@"; do case "$a" in {pattern}) {mark}; echo \'{printed}\'; exit 1;;'
                " esac; done\n" + real
            )
        elif kind.startswith("replaces:"):
            # 指定した形のパスだけ別のパスに解決したことにして、成功の終了コードを返す。
            # 他の引数は 1 件ずつ本物で解決し、入力 1 件につき 1 行の対応を保つ。
            # 形式: replaces:<出力する値>::<case のパターン>
            printed, pattern = kind.removeprefix("replaces:").split("::", 1)
            body = (
                'opts=""\n'
                'while [ $# -gt 0 ]; do case "$1" in\n'
                '  --) shift; break;;\n'
                '  -*) opts="$opts $1"; shift;;\n'
                "  *) break;;\n"
                "esac; done\n"
                'for a in "$@"; do case "$a" in\n'
                f"  {pattern}) {mark}; printf '%s\\n' '{printed}';;\n"
                f"  *) '{GNU_REALPATH}' $opts -- \"$a\" || exit 1;;\n"
                "esac; done\n"
            )
        elif kind.startswith("empty-on:"):
            # 指定したパスの解決だけ、何も出力せずに成功の終了コードを返す。
            pattern = kind.removeprefix("empty-on:")
            body = f'for a in "$@"; do case "$a" in {pattern}) {mark}; exit 0;; esac; done\n' + real
        elif kind == "absent":
            return
        else:
            raise ValueError(kind)
        self.make_stub(bin_dir, name, "#!/bin/sh\n" + body)

    def variant_env(
        self,
        *,
        jq: str = "real",
        realpath: str = "real",
        grealpath: str = "absent",
        without: frozenset[str] = frozenset(),
        extra_stubs: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """依存コマンドを差し替えた PATH。切り替え対象は bin_dir のスタブからしか見えない。"""
        self.variant_count += 1
        bin_dir = self.root / f"variant-bin-{self.variant_count}"
        bin_dir.mkdir()
        self.place_jq(bin_dir, jq)
        self.place_realpath(bin_dir, "realpath", realpath)
        self.place_realpath(bin_dir, "grealpath", grealpath)
        for name, body in (extra_stubs or {}).items():
            self.make_stub(bin_dir, name, "#!/bin/sh\n" + body)
        commands = self.make_wrapper_dir(
            self.root / f"variant-commands-{self.variant_count}",
            omit=tuple(SWITCHED_COMMANDS | without),
        )
        self.variant_bin = bin_dir
        return self.hook_env(PATH=f"{self.posix(bin_dir)}:{commands}")

    def working_env(self) -> dict[str, str]:
        """依存コマンドがすべて動く環境。各ケースの判定の基準にする。"""
        return self.variant_env()

    def range_pattern(self, *relative: str) -> str:
        """隔離ツリー内の場所に当たる case パターン。

        ツリーの位置の表記（/tmp/...・/c/Users/...）にも区切り文字（/ と円記号）にも
        依存させない。Windows では BASH_SOURCE などが円記号区切りのまま渡りうる。
        """
        separator = "[/" + chr(92) * 2 + "]"

        def one(part: str) -> str:
            segments = [self.root.name, *part.split("/")]
            return "*" + separator + separator.join(f'"{s}"' if s else "" for s in segments)

        return "|".join(one(part) for part in relative)

    def decision_matrix(self) -> list[tuple[str, Path, bool]]:
        """依存コマンドがすべて動く環境での許可・拒否の判定表。"""
        matrix = [
            ("a project file", self.project / "docs" / "note.md", True),
            ("a new nested project file", self.project / "new" / "deeper" / "note.md", True),
            ("a skill file", self.skills / "tdd" / "SKILL.md", True),
            ("a memory file", self.memory / "MEMORY.md", True),
            ("a scratchpad file", self.scratchpad / "nested" / "note.md", True),
            ("skills beside the hook", self.root / "skills" / "tdd" / "SKILL.md", True),
            ("a file under the home", self.home / "other" / "note.md", False),
            ("a dot-dot escape", self.project / ".." / "outside" / "note.md", False),
            ("a file under the cwd", self.elsewhere / "note.md", False),
        ]
        if SHELL_IS_MSYS:
            matrix.append(
                (
                    "a scratchpad file on the base without a uid",
                    self.tmpdir / "claude" / self.slug / SESSION_ID / "s" / "note.md",
                    True,
                )
            )
        if self.has_escape_link:
            matrix.append(
                ("an escape through a symlink", self.scratchpad / "escape" / "note.md", False)
            )
        return matrix

    def spellings(self, target: Path) -> dict[str, str]:
        """依存コマンドの欠落を確かめるときに渡す表記。Windows では Windows 表記も含める。"""
        forms = {"posix": self.posix(target)}
        if IS_WINDOWS:
            forms["backslash"] = str(target)
        return forms

    # -- assertions --------------------------------------------------------

    def assert_failure_was_exercised(self) -> None:
        """スタブに仕込んだ失敗が実際に起きたこと。起きていなければケースは空振りしている。"""
        assert self.variant_bin is not None
        self.assertTrue(
            (self.variant_bin / "triggered").exists(),
            "the stubbed failure was never reached; the case does not test what it names",
        )

    # -- tests -------------------------------------------------------------

    def test_harness_switches_the_dependency_commands(self):
        # Arrange
        env = self.variant_env(jq="absent", realpath="no-m", grealpath="absent")
        assert self.variant_bin is not None
        bin_dir = self.posix(self.variant_bin)

        # Act
        probe = subprocess.run(
            [
                BASH_PATH,
                "-c",
                "command -v jq; command -v grealpath; command -v cat >/dev/null && echo has-cat;"
                " type -ap realpath; realpath -m /x; realpath /",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        # Assert
        self.assertEqual(probe.stdout, f"has-cat\n{bin_dir}/realpath\n/\n", probe)
        self.assertIn("illegal option -- m", probe.stderr)

    def test_stops_with_exit_2_when_jq_does_not_work(self):
        targets: dict[str, str | None] = {"a payload without file_path": None}
        for label, target in (
            ("a project file", self.project / "docs" / "note.md"),
            ("a file outside the project", self.home / "other" / "note.md"),
        ):
            for notation, spelling in self.spellings(target).items():
                targets[f"{label} ({notation})"] = spelling
        for jq in ("absent", "failing"):
            for label, spelling in targets.items():
                with self.subTest(jq=jq, case=label):
                    # Act
                    run = self.check(spelling, env=self.variant_env(jq=jq), cwd=self.elsewhere)

                    # Assert
                    self.assert_stopped_with_exit_2(run, self.JQ_NEEDLES)

    def test_stops_with_exit_2_when_no_realpath_accepts_m(self):
        targets: dict[str, str] = {}
        for label, target in (
            ("a project file", self.project / "docs" / "note.md"),
            ("a file outside the project", self.home / "other" / "note.md"),
            ("a scratchpad file", self.scratchpad / "note.md"),
        ):
            for notation, spelling in self.spellings(target).items():
                targets[f"{label} ({notation})"] = spelling
        for realpath in ("absent", "no-m"):
            for label, spelling in targets.items():
                with self.subTest(realpath=realpath, grealpath="absent", case=label):
                    # Act
                    run = self.check(
                        spelling, env=self.variant_env(realpath=realpath), cwd=self.elsewhere
                    )

                    # Assert
                    self.assert_stopped_with_exit_2(run, self.REALPATH_NEEDLES)

    def test_falls_back_to_grealpath_with_the_same_decisions(self):
        for realpath in ("no-m", "absent"):
            env = self.variant_env(realpath=realpath, grealpath="real")
            for label, target, allowed in self.decision_matrix():
                with self.subTest(realpath=realpath, case=label):
                    # Arrange
                    working = self.check(target, env=self.working_env(), cwd=self.elsewhere)

                    # Act
                    fallback = self.check(target, env=env, cwd=self.elsewhere)

                    # Assert
                    self.assert_decision(working, allowed=allowed)
                    self.assertEqual(fallback, working)

    def test_stops_with_exit_2_when_the_deny_decision_cannot_be_written(self):
        # Arrange: 拒否の理由が違っても、拒否を JSON で出せなければ exit 2 で止める。
        backslash = chr(92)
        denied_cases: dict[str, str | Path] = {
            "a file outside every base": self.home / "other" / "note.md",
            "a unc path": "//server/share/note.md",
            "a unc path with backslashes": backslash * 2 + "server" + backslash + "note.md",
            "a relative path": "docs/note.md",
        }
        for label, target in denied_cases.items():
            with self.subTest(case=label):
                env = self.variant_env(jq="fails-on-deny")

                # Act
                run = self.check(target, env=env, cwd=self.project)

                # Assert
                self.assert_stopped_with_exit_2(run, ())
                self.assert_failure_was_exercised()

        # Act / Assert: 許可の判定は JSON を出さないので、この jq でも通る。
        allowed = self.check(
            self.project / "docs" / "note.md",
            env=self.variant_env(jq="fails-on-deny"),
            cwd=self.elsewhere,
        )
        self.assert_allowed(allowed)

    def test_does_not_treat_an_unresolved_range_as_allowing_everything(self):
        # Arrange: 許可範囲の解決だけが途中で失敗する。空の範囲は "/*" と同じ意味になりうる。
        failures = {
            "the skills ranges": self.range_pattern(
                "home/.claude/skills", "copy/../skills", "skills"
            ),
            "the project root": self.range_pattern("project", "project/"),
        }
        for label, pattern in failures.items():
            with self.subTest(unresolved=label):
                env = self.variant_env(realpath=f"fails-on:{pattern}")

                # Act
                run = self.check(self.home / "other" / "note.md", env=env, cwd=self.elsewhere)

                # Assert
                self.assert_refused(run)
                self.assert_failure_was_exercised()

    def test_does_not_derive_a_range_from_the_cwd_when_the_hook_path_cannot_be_resolved(self):
        # Arrange: フック自身のパスの解決だけが失敗する。空のパスの親は "." や "" になり、
        # その ../skills は cwd の親の skills や /skills を指してしまう。
        cwd = self.root / "x" / "proj"
        cwd.mkdir(parents=True)
        env = self.variant_env(realpath=f"fails-on:{self.range_pattern(f'copy/{HOOK_NAME}')}")

        # Act
        run = self.check(self.root / "x" / "skills" / "a.md", env=env, cwd=cwd)

        # Assert
        self.assert_refused(run)
        self.assert_failure_was_exercised()

    def test_refuses_when_the_target_path_cannot_be_resolved(self):
        # Arrange
        target = self.project / "docs" / "note.md"
        env = self.variant_env(realpath=f"fails-on:{self.range_pattern('project/docs/note.md')}")

        # Act
        run = self.check(target, env=env, cwd=self.elsewhere)

        # Assert
        self.assert_refused(run)
        self.assert_failure_was_exercised()

    def test_does_not_open_the_root_skills_when_the_hook_resolves_next_to_the_root(self):
        """フック自身のパスが、親を取るとルートになる場所に解決されるとき、/skills 配下への書き込みは許可されない。"""
        resolutions = {
            "a file directly under the root": "/h",
            "the root itself": "/",
        }
        if not SHELL_IS_MSYS:
            # MSYS では realpath の後に cygpath -w が続き、空のパスはそこで失敗して
            # このケースが確かめたい分岐まで届かない。
            resolutions["an empty path"] = ""
        pattern = self.range_pattern(f"copy/{HOOK_NAME}")
        for label, printed in resolutions.items():
            with self.subTest(hook_resolves_to=label):
                # Arrange: 差し替えた realpath が他の引数を本物どおりに解決すること。
                variant = f"replaces:{printed}::{pattern}"
                self.assert_allowed(
                    self.check(
                        self.project / "docs" / "note.md",
                        env=self.variant_env(realpath=variant),
                        cwd=self.elsewhere,
                    )
                )
                env = self.variant_env(realpath=variant)

                # Act
                run = self.check("/skills/evil.sh", env=env, cwd=self.elsewhere)

                # Assert
                self.assert_refused(run)
                self.assert_failure_was_exercised()

    @unittest.skipUnless(SHELL_IS_MSYS, "cygpath is only used on MSYS/Cygwin")
    def test_refuses_when_only_the_first_cygpath_for_the_repository_skills_fails(self):
        """リポジトリの skills を求める正規化で、最初の cygpath だけが失敗した場合、許可範囲の外のパスへの書き込みは拒否される。"""
        # Arrange: 書き込み先と基点の正規化はすべて通り、フックの隣の ../skills を
        # 変換する呼び出しだけが失敗する。
        cygpath = TOOL_PATHS["cygpath"]
        pattern = self.range_pattern("copy/../skills")
        drive_root = "/" + str(self.root)[0].lower()
        targets: dict[str, str | Path] = {
            "a file outside every base": self.outside / "note.md",
            "a system file": f"{drive_root}/Windows/x",
        }
        for index, (label, target) in enumerate(targets.items()):
            with self.subTest(case=label):
                marker = self.root / f"cygpath-triggered-{index}"
                stub = (
                    f'for a in "$@"; do case "$a" in {pattern})'
                    f" : > '{self.posix(marker)}'; exit 1;; esac; done\n"
                    f"exec '{cygpath}' \"$@\"\n"
                )
                env = self.variant_env(extra_stubs={"cygpath": stub})

                # Act
                run = self.check(target, env=env, cwd=self.elsewhere)

                # Assert
                self.assert_refused(run)
                self.assertTrue(
                    marker.exists(),
                    "the stubbed cygpath failure was never reached; the case is vacuous",
                )

    # -- 許可範囲ごとの解決の失敗 ----------------------------------------------

    def range_resolutions(self) -> dict[str, tuple[str, str, str]]:
        """許可範囲ごとに (その範囲の解決に当たる case パターン, 範囲が空になると許可される
        パス, 範囲が /etc になると許可されるパス)。どちらのパスも本来は拒否される。
        """
        scratch = f"tmp/claude-{CURRENT_UID}"
        resolutions = {
            "the project root": (
                self.range_pattern("project", "project/"),
                "/etc/passwd",
                "/etc/passwd",
            ),
            "the skills directory": (
                self.range_pattern("home/.claude/skills", "home/.claude/skills/"),
                "/etc/passwd",
                "/etc/passwd",
            ),
            "the hook path": (
                self.range_pattern(f"copy/{HOOK_NAME}"),
                "/skills/evil.sh",
                "/etc/skills/evil.sh",
            ),
            "the repository skills": (
                self.range_pattern("copy/../skills", "skills", "skills/"),
                "/etc/passwd",
                "/etc/passwd",
            ),
            "the home directory": (
                self.range_pattern("home", "home/"),
                "/.claude/projects/x/memory/a.md",
                "/etc/.claude/projects/x/memory/a.md",
            ),
            "the scratchpad base": (
                self.range_pattern(scratch, f"{scratch}/"),
                f"/slug/{SESSION_ID}/note.md",
                f"/etc/slug/{SESSION_ID}/note.md",
            ),
        }
        if SHELL_IS_MSYS:
            resolutions["the scratchpad base without a uid"] = (
                self.range_pattern("tmp/claude", "tmp/claude/"),
                f"/slug/{SESSION_ID}/note.md",
                f"/etc/slug/{SESSION_ID}/note.md",
            )
        return resolutions

    def test_refuses_when_a_range_fails_to_resolve_without_output(self):
        for label, (pattern, target, _) in self.range_resolutions().items():
            with self.subTest(range=label):
                # Arrange
                env = self.variant_env(realpath=f"fails-on:{pattern}")

                # Act
                run = self.check(target, env=env, cwd=self.elsewhere)

                # Assert
                self.assert_refused(run)
                self.assert_failure_was_exercised()

    def test_refuses_when_a_range_fails_to_resolve_but_prints_a_path(self):
        # Arrange: 失敗の終了コードと一緒に出力された値を、範囲として使ってはならない。
        # 書き込み先そのものは、失敗時にプロジェクト内のパスを出力させる。
        cases: dict[str, tuple[str, str, str]] = {
            "the target path": ('"/etc/passwd"', self.posix(self.project / "x"), "/etc/passwd"),
        }
        for label, (pattern, _, target) in self.range_resolutions().items():
            printed = "/etc/hookdir/hook.sh" if label == "the hook path" else "/etc"
            cases[label] = (pattern, printed, target)
        for label, (pattern, printed, target) in cases.items():
            with self.subTest(range=label):
                env = self.variant_env(realpath=f"fails-printing:{printed}::{pattern}")

                # Act
                run = self.check(target, env=env, cwd=self.elsewhere)

                # Assert
                self.assert_refused(run)
                self.assert_failure_was_exercised()

    def test_refuses_when_a_range_resolves_to_an_empty_string(self):
        # Arrange: 成功の終了コードで空を返す realpath。空の範囲は "/*" と同じ意味になりうる。
        for label, (pattern, target, _) in self.range_resolutions().items():
            with self.subTest(range=label):
                env = self.variant_env(realpath=f"empty-on:{pattern}")

                # Act
                run = self.check(target, env=env, cwd=self.elsewhere)

                # Assert
                self.assert_refused(run)
                self.assert_failure_was_exercised()

    # -- 名前を挙げていないコマンド ----------------------------------------------

    def test_stops_with_exit_2_when_the_payload_cannot_be_read(self):
        targets = {
            "a project file": self.project / "docs" / "note.md",
            "a file outside the project": self.home / "other" / "note.md",
        }
        variants: dict[str, dict[str, object]] = {
            "cat is missing": {"without": frozenset({"cat"})},
            "cat fails after partial output": {
                "extra_stubs": {"cat": "printf '{}'\nexit 1\n"},
            },
        }
        for variant, options in variants.items():
            for label, target in targets.items():
                with self.subTest(variant=variant, case=label):
                    # Act
                    run = self.check(
                        target,
                        env=self.variant_env(**options),  # type: ignore[arg-type]
                        cwd=self.elsewhere,
                    )

                    # Assert
                    self.assert_stopped_with_exit_2(run, ())

    def test_decides_the_same_without_dirname(self):
        # Arrange
        env = self.variant_env(without=frozenset({"dirname"}))
        cwd = self.root / "x" / "proj"
        cwd.mkdir(parents=True)

        for label, target, allowed in self.decision_matrix():
            with self.subTest(case=label):
                # Act
                working = self.check(target, env=self.working_env(), cwd=self.elsewhere)
                without = self.check(target, env=env, cwd=self.elsewhere)

                # Assert
                self.assert_decision(working, allowed=allowed)
                self.assertEqual(without, working)

        escapes: dict[str, tuple[str | Path, Path]] = {
            "the root-level skills": ("/skills/evil.sh", self.elsewhere),
            "skills relative to the cwd": (self.root / "x" / "skills" / "a.md", cwd),
        }
        for label, (target, run_cwd) in escapes.items():
            with self.subTest(case=label):
                # Act / Assert
                self.assert_refused(self.check(target, env=env, cwd=run_cwd))

    def test_does_not_allow_the_scratchpad_when_session_id_extraction_fails(self):
        # Arrange: jq は session_id の取り出しでだけ、正しい値を出力して失敗する。
        cases: dict[str, Path] = {"the scratchpad": self.scratchpad / "note.md"}
        if SHELL_IS_MSYS:
            cases["the scratchpad on the base without a uid"] = (
                self.tmpdir / "claude" / self.slug / SESSION_ID / "s" / "note.md"
            )
        for label, target in cases.items():
            with self.subTest(case=label):
                env = self.variant_env(jq="fails-on-session-id")

                # Act
                run = self.check(target, env=env, cwd=self.elsewhere)

                # Assert
                self.assert_refused(run)
                self.assert_failure_was_exercised()
                self.assert_allowed(
                    self.check(target, env=self.working_env(), cwd=self.elsewhere)
                )

    # -- stdin の読み切り --------------------------------------------------------

    def feed_hook(self, hook: Path, stdin: bytes, env: dict[str, str]) -> tuple[HookRun, str]:
        """stdin を別スレッドで書き込みながら実行し、書き込みの失敗（パイプの切断）も返す。

        communicate() は書き込み中の BrokenPipeError を握りつぶすため、ここでは自前で書く。
        パイプの容量（Linux では 64KiB）を超える入力は、相手が読まない限り書き終わらない。
        相手が読まずに終了したときの例外は OS で異なる（Linux は BrokenPipeError、
        Windows は EINVAL の OSError）ため、例外の型名をそのまま返す。
        """
        self.abort_if_environment_escapes_tree(env, self.elsewhere)
        popen_kwargs: dict[str, object] = {}
        if not IS_WINDOWS:
            popen_kwargs["start_new_session"] = True
        with self.real_environment_unchanged():
            process = subprocess.Popen(
                [BASH_PATH, str(hook)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(self.elsewhere),
                **popen_kwargs,  # type: ignore[arg-type]
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
                stream = getattr(process, name)
                outputs[name] = stream.read()

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
                # 書き込みに失敗した stdin は、未送出のデータを抱えたまま閉じると例外になる。
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
        run = HookRun(
            process.returncode,
            outputs.get("stdout", b"").decode("utf-8", "replace"),
            outputs.get("stderr", b"").decode("utf-8", "replace"),
        )
        return run, ",".join(write_error)

    def large_payload(self, target: Path) -> bytes:
        payload = json.loads(self.payload(target))
        payload["padding"] = "x" * (1 << 20)
        return json.dumps(payload).encode("utf-8")

    def test_harness_detects_a_hook_that_exits_without_reading_stdin(self):
        # Arrange
        quitter = self.make_stub(self.root, "quitter.sh", "#!/bin/sh\nexit 2\n")

        # Act
        _, write_error = self.feed_hook(
            quitter, self.large_payload(self.home / "other" / "note.md"), self.working_env()
        )

        # Assert
        self.assertNotEqual(write_error, "", "the harness cannot observe an unread stdin")

    def test_reads_a_large_payload_to_the_end_before_stopping(self):
        variants: dict[str, dict[str, str]] = {
            "jq is missing": {"jq": "absent"},
            "no realpath accepts -m": {"realpath": "absent"},
            "all dependencies work": {},
        }
        for label, options in variants.items():
            with self.subTest(case=label):
                # Arrange
                stdin = self.large_payload(self.home / "other" / "note.md")

                # Act
                run, write_error = self.feed_hook(self.hook, stdin, self.variant_env(**options))

                # Assert
                self.assertEqual(write_error, "", f"the hook did not read stdin: {run!r}")
                if options:
                    self.assert_stopped_with_exit_2(run, ())
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

    def test_denies_an_unsupported_notation_with_a_printable_reason(self):
        # Arrange: 制御文字を含む名前。ソースにはリテラルで埋め込まない。
        name = "we" + chr(10) + "ir" + chr(1) + "d.md"
        cases = {
            "a relative path": name,
            "a unc path": "//server/share/" + name,
        }

        for label, spelling in cases.items():
            with self.subTest(case=label):
                # Act
                reason = self.assert_path_denied(spelling, cwd=self.elsewhere)

                # Assert
                self.assertIn("weird.md", reason)
                leaked = sorted(CONTROL_CHARACTERS & set(reason))
                self.assertEqual(
                    leaked, [], f"control characters leaked into the reason: {reason!r}"
                )


if __name__ == "__main__":
    unittest.main()
