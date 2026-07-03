---
name: shared-memory
description: Claude Code / Codex 横断で共有するローカルメモリ（SQLite）への保存と読み出し。Use when user says "覚えておいて", "記録しておいて", "前回の作業", "共有メモリ", or "/shared-memory". Also use proactively: ツール横断で長期的に有効な事実（ユーザーの好み・環境・決定事項）を学んだとき保存し、大きな作業の開始時や過去の経緯が必要なときに読み出す。
---

# Shared Memory

Claude Code と Codex の間で共有するローカル記憶（SQLite）を、必要と判断したときに自分で読み書きするためのスキル。毎ターン自動で書き込む仕組みは廃止されており、保存・読み出しはこのスキル経由でのみ行う。

## 基盤情報

- DB パス: `~/.agents/memory/memory.db`
- CLI: `python3 ~/.agents/memory/memory.py`

## 保存フロー（write-memory）

`write-memory` は既存の `session_id` に紐づく event を作ってから observation → memory を作る（FK 制約があるため、存在しない session_id を渡すとエラーになる）。会話中のセッション ID を把握していない場合は、先に `start-session` で仮のセッションを作ってから書き込む。

```bash
# 1. セッションがなければ作成（初回のみ）
python3 ~/.agents/memory/memory.py start-session \
  --client claude-code --user-id default --project-id my-project \
  --session-id manual-$(date +%s)

# 2. 記憶を書き込む
python3 ~/.agents/memory/memory.py write-memory \
  --session-id manual-1234567890 \
  --memory-type semantic \
  --key preferred_editor \
  --summary "ユーザーは vim キーバインドを好む" \
  --confidence 0.9 \
  --scope global
```

主なオプション（`write-memory --help` で確認済み）:
- `--session-id`（必須）: 紐づけるセッション ID
- `--memory-type`（必須）: `semantic`（事実・嗜好）/ `procedural`（行動ルール）/ `episodic`（重要な出来事）
- `--key` / `--summary`（必須）: 検索キーと日本語での要約
- `--confidence`: デフォルト 0.8。明示的発言は 0.8〜1.0、推測は 0.5〜0.7 程度
- `--scope`: `global`（全プロジェクト共通、デフォルト）/ `project`（特定プロジェクト、`--project-id` も指定）
- `--entity-type` / `--entity-id`: デフォルト `user` / `default`。プロジェクト固有の記憶なら `project` / project 名

## 読み出しフロー

### get-context（作業開始時の文脈取得）

```bash
python3 ~/.agents/memory/memory.py get-context --user-id default --project-id my-project
```

`active` な記憶を `procedural` / `semantic` / `episodic` に分類して返す。大きな作業に着手する前にまず実行する。

### search（キーワード検索）

```bash
python3 ~/.agents/memory/memory.py search --query 'vim' --project-id my-project
```

`active` な記憶のみが対象。`--memory-type` / `--scope` / `--entity-id` / `--limit` で絞り込み可能（`search --help` 参照）。

### history（過去セッション横断検索）

```bash
python3 ~/.agents/memory/memory.py history --query 'vim'
```

`memories` / `sessions` / `events` を横断して過去の経緯を掘る。`--no-memories` / `--no-sessions` / `--no-events` で対象を絞れる（`history --help` 参照）。

**注意**: `--project-id` を付けると `scope=global` の記憶（`project_id` が NULL）は除外されるため一致しない。global スコープの記憶を含めて検索したい場合は `--project-id` を省略する。

## 削除（forget）

```bash
python3 ~/.agents/memory/memory.py forget --memory-id mem_xxxx --reason "ユーザー環境が変わったため"
```

`--memory-id` と `--reason`（いずれも必須）を指定して `status` を `deleted` にする。

## 保存基準

保存するのは「長期的に有効」かつ「ツール横断で有用」な事実のみ。

**保存する**:
- ユーザーの技術的な好み（エディタ、言語、フレームワーク等）
- 開発環境・ツールの設定
- 繰り返し参照するプロジェクト固有の決定事項

**保存しない**:
- 会話限りの一時的な情報（そのセッションだけで完結する作業内容）
- リポジトリに既に記録されている事実（コード構造・git 履歴・CLAUDE.md 記載事項）
- Claude Code 専用の文脈（標準メモリ機構の担当領域であり、ツール横断の共有メモリで扱う必要はない）

## 関連スキル

- 基盤自体の不具合調査・hook やスキーマの確認: `/memory`
- セッション履歴からの一括抽出: `/memory-extract`
