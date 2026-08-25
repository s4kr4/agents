---
name: setup-agent-rules
description: Claude Code と Codex が同じプロジェクトルールを参照する配置を安全に初期化・検証・保守する。docs/rules を共通ルールの正本にしたい、.claude/rules と AGENTS.md・CLAUDE.md の参照を整備したい、既存配置を検査したい、またはプロジェクトルールを追加・編集・削除・整理したいときに使う。
---

# Setup Agent Rules

プロジェクト固有の指示を `docs/rules/` に集約し、Claude Code と Codex の両方から参照できる状態を初期化・保守する。

## 手順

1. 対象プロジェクトのルートを確定し、実行前に対象を明示する。
2. `validate` を実行する。成功した場合は変更しない。
3. 未導入または不完全なら `init` を実行する。
4. `validate` を再実行し、成功を確認する。
5. 追加ルールは `docs/rules/*.md` に置き、`docs/rules/INDEX.md` に適用条件を追記する。

```bash
python scripts/setup_agent_rules.py validate --project /path/to/project
python scripts/setup_agent_rules.py init --project /path/to/project
python scripts/setup_agent_rules.py validate --project /path/to/project
```

スクリプトのパスは、この `SKILL.md` を基準に解決する。

## ルールの追加・編集

プロジェクトルールを追加・編集・削除・整理するときも、次の手順に従う。

1. 対象プロジェクトで `validate` を実行し、共通ルール配置が正常であることを確認する。
2. `docs/rules/INDEX.md` と既存の `docs/rules/*.md` を読み、重複・矛盾・適用範囲を確認する。
3. ルール本文は `docs/rules/` だけで変更する。`.claude/rules/` 側にはファイルを作成・編集しない。
4. 1ファイル1トピックを基本とし、内容を端的で検証可能な指示として記述する。
5. `docs/rules/INDEX.md` の一覧と適用条件を同じ変更で更新する。ルールを削除・改名した場合も参照を残さない。
6. Claude Code の条件付き適用が必要なら、ルールファイルに `paths` frontmatterを付ける。同じ適用条件を、Codexが判断できる言葉で `INDEX.md` にも記載する。
7. 変更後に `validate` を再実行し、差分を確認する。

常時必要な短い指示は `AGENTS.md` に置き、対象ファイルやタスクが限定される指示は `docs/rules/` に置く。繰り返し実行する手順や長い参考資料は、ルールではなくスキルへの分離を検討する。

## 安全性

- 既存の `AGENTS.md` と `CLAUDE.md` を保持し、必要な参照がない場合だけ末尾へ通常の Markdown として追記する。
- `AGENTS.md` は `docs/rules/INDEX.md` への参照、`CLAUDE.md` は独立した行の `@AGENTS.md` を内容ベースで検出し、重複して追記しない。
- 既存の同名見出しや周辺の文書は置換・削除しない。初期化と構成検証だけを行い、追記後の内容を自動管理しない。
- `.claude/rules` が期待どおりの相対リンクでなければ停止する。ファイル、実ディレクトリ、別リンクを置換しない。
- `docs/rules/INDEX.md` が既にあれば変更しない。
- 実行後に差分を確認し、既存ルールとの優先順位や重複を報告する。

```text
project/
├── AGENTS.md
├── CLAUDE.md
├── docs/rules/INDEX.md
└── .claude/rules -> ../docs/rules
```
