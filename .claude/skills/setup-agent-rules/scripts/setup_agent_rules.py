#!/usr/bin/env python3
"""Claude Code と Codex の共通プロジェクトルール配置を初期化・検証する。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


EXPECTED_LINK = Path("../docs/rules")

AGENTS_CONTENT = """## プロジェクトルール

作業を開始する前に `docs/rules/INDEX.md` を確認すること。
INDEX に記載されたルールのうち、現在のタスクおよび変更対象ファイルに
該当するものを読み、その指示に従うこと。
"""

CLAUDE_CONTENT = "@AGENTS.md\n"

INDEX_CONTENT = """# プロジェクトルール

このディレクトリは Claude Code と Codex が共有するプロジェクトルールの正本です。

## ルール一覧

| ルール | 適用対象 |
| --- | --- |
| （ルールを追加してください） | （適用条件を記載してください） |
"""


class SetupError(Exception):
    """安全に初期化できない状態を表す。"""


def parse_args() -> argparse.Namespace:
    """CLI引数を解析する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "validate"))
    parser.add_argument(
        "--project",
        type=Path,
        default=Path.cwd(),
        help="対象プロジェクトのルート（既定: カレントディレクトリ）",
    )
    return parser.parse_args()


def contains_required_reference(
    path: Path, required: str, *, standalone_line: bool = False
) -> bool:
    """ファイルを安全に検査し、必要な参照の有無を返す。"""
    if path.is_symlink():
        raise SetupError(f"競合: {path} はシンボリックリンクです")
    if not path.exists():
        return False
    if not path.is_file():
        raise SetupError(f"競合: {path} は通常ファイルではありません")
    content = path.read_text(encoding="utf-8")
    if standalone_line:
        return any(line.strip() == required for line in content.splitlines())
    return required in content


def preflight(project: Path) -> tuple[bool, bool]:
    """書き込み前に全競合を検査する。"""
    if not project.is_dir():
        raise SetupError(f"対象プロジェクトがディレクトリではありません: {project}")
    docs_directory = project / "docs"
    if docs_directory.is_symlink():
        raise SetupError(f"競合: {docs_directory} はシンボリックリンクです")
    if docs_directory.exists() and not docs_directory.is_dir():
        raise SetupError(f"競合: {docs_directory} はディレクトリではありません")
    rules_directory = docs_directory / "rules"
    if rules_directory.is_symlink():
        raise SetupError(f"競合: {rules_directory} はシンボリックリンクです")
    if rules_directory.exists() and not rules_directory.is_dir():
        raise SetupError(f"競合: {rules_directory} はディレクトリではありません")
    index = rules_directory / "INDEX.md"
    if index.is_symlink():
        raise SetupError(f"競合: {index} はシンボリックリンクです")
    if index.exists() and not index.is_file():
        raise SetupError(f"競合: {index} は通常ファイルではありません")
    claude_directory = project / ".claude"
    if claude_directory.is_symlink():
        raise SetupError(f"競合: {claude_directory} はシンボリックリンクです")
    if claude_directory.exists() and not claude_directory.is_dir():
        raise SetupError(f"競合: {claude_directory} はディレクトリではありません")
    rules_link = claude_directory / "rules"
    if rules_link.is_symlink():
        if rules_link.readlink() != EXPECTED_LINK:
            raise SetupError(
                f"競合: {rules_link} は期待しないリンク先です: {rules_link.readlink()}"
            )
    elif rules_link.exists():
        raise SetupError(f"競合: {rules_link} が既に存在します。置換しません")
    agents_configured = contains_required_reference(
        project / "AGENTS.md", "docs/rules/INDEX.md"
    )
    claude_configured = contains_required_reference(
        project / "CLAUDE.md", "@AGENTS.md", standalone_line=True
    )
    return agents_configured, claude_configured


def append_content(path: Path, additional_content: str) -> None:
    """既存内容を維持し、区切りを整えて内容を追記する。"""
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if content and not content.endswith("\n"):
        content += "\n"
    if content:
        content += "\n"
    path.write_text(content + additional_content, encoding="utf-8")


def initialize(project: Path) -> None:
    """競合がない場合だけ共通ルール配置を初期化する。"""
    agents_configured, claude_configured = preflight(project)
    rules_directory = project / "docs/rules"
    rules_directory.mkdir(parents=True, exist_ok=True)
    index = rules_directory / "INDEX.md"
    if not index.exists():
        index.write_text(INDEX_CONTENT, encoding="utf-8")

    claude_directory = project / ".claude"
    claude_directory.mkdir(exist_ok=True)
    rules_link = claude_directory / "rules"
    if not rules_link.is_symlink():
        rules_link.symlink_to(EXPECTED_LINK)
    if not agents_configured:
        append_content(project / "AGENTS.md", AGENTS_CONTENT)
    if not claude_configured:
        append_content(project / "CLAUDE.md", CLAUDE_CONTENT)


def validation_errors(project: Path) -> list[str]:
    """配置の検証エラーを返す。"""
    errors: list[str] = []
    preflight(project)
    index = project / "docs/rules/INDEX.md"
    if not index.exists():
        errors.append(f"不足: {index}")
    rules_link = project / ".claude/rules"
    if not rules_link.is_symlink():
        errors.append(f"不足: {rules_link} はシンボリックリンクではありません")
    elif rules_link.readlink() != EXPECTED_LINK:
        errors.append(f"不正: {rules_link} -> {rules_link.readlink()}")
    for filename, required, standalone_line in (
        ("AGENTS.md", "docs/rules/INDEX.md", False),
        ("CLAUDE.md", "@AGENTS.md", True),
    ):
        path = project / filename
        if not path.exists():
            errors.append(f"不足: {path}")
            continue
        if not contains_required_reference(
            path, required, standalone_line=standalone_line
        ):
            errors.append(f"不正: {path} に必要な参照がありません: {required}")
    return errors


def main() -> int:
    """CLIエントリーポイント。"""
    args = parse_args()
    project = args.project.resolve()
    try:
        if args.command == "init":
            initialize(project)
            print(f"初期化しました: {project}")
            return 0
        errors = validation_errors(project)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(f"検証に成功しました: {project}")
        return 0
    except (OSError, UnicodeError, SetupError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
