---
name: shared-memory
description: Claude Code / Codex 横断で共有するローカルメモリ（ファイルベース）への保存と読み出し。Use when user says "覚えておいて", "記録しておいて", "前回の作業", "共有メモリ", or "/shared-memory". Also use proactively: ツール横断で長期的に有効な事実（ユーザーの好み・環境・決定事項）を学んだとき保存し、大きな作業の開始時や過去の経緯が必要なときに読み出す。
---

# Shared Memory

Claude Code と Codex の間で共有するローカル記憶を、必要と判断したときに自分で読み書きするためのスキル。毎ターン自動で書き込む仕組みは廃止されており、保存・読み出しはこのスキル経由でのみ行う。

## 基盤情報

ファイルベースの 2 層構成（詳細は `/memory` スキル参照）。

- Vault（memories・Syncthing 同期対象）: `$LLM_MEMORY_VAULT/memory/`（未設定時は `~/.agents/memory/vault/` にフォールバック）
- local（sessions/events/observations・同期対象外）: `$LLM_MEMORY_LOCAL_DIR`（既定 `~/.agents/memory/local/`）
- CLI: `python3 ~/.agents/memory/memory.py`

## Vault の配置原則

- `memory/` 直下に置く Markdown ファイルは生成された `_index.md` のみとする。
- 個別の記憶は scope に対応する分類ディレクトリ（例: `memory/global/`、`memory/projects/<project-slug>/`）内へ保存する。
- 記憶ファイルを Vault 直下へ手作業で作成しない。CLI の `write-memory` を使い、分類・索引更新を任せる。

## 保存フロー（write-memory）

`write-memory` は `session_id` に紐づく event を作ってから observation → memory を作る。会話中のセッション ID を把握していない場合は、先に `start-session` で仮のセッションを作ってから書き込む。

```bash
# 1. セッションがなければ作成（初回のみ）
python3 ~/.agents/memory/memory.py start-session \
  --client claude-code --user-id default --project-id my-project \
  --session-id manual-$(date +%s)

# 2. 記憶を書き込む
python3 ~/.agents/memory/memory.py write-memory \
  --session-id manual-1234567890 \
  --memory-type profile \
  --key preferred_editor \
  --summary "ユーザーは vim キーバインドを好む" \
  --confidence 0.9 \
  --scope global
```

主なオプション（`write-memory --help` で確認済み）:
- `--session-id`（必須）: 紐づけるセッション ID
- `--memory-type`（必須）: `profile`（静的事実・嗜好）/ `feedback`（協働のしかたの学び）/ `reference`（外部システムへのポインタ・プロジェクト固有の決定事項）
- `--key` / `--summary`（必須）: 検索キーと日本語での要約
- `--confidence`: デフォルト 0.8。明示的発言は 0.8〜1.0、推測は 0.5〜0.7 程度
- `--scope`: `global`（全プロジェクト共通、デフォルト）/ `project`（特定プロジェクト、`--project-id` も必須指定。`--project-id` を省略するとエラーになる）
- `--entity-type` / `--entity-id`: デフォルト `user` / `default`。プロジェクト固有の記憶なら `project` / project 名

## 読み出しフロー

### get-context（作業開始時の文脈取得）

```bash
python3 ~/.agents/memory/memory.py get-context --user-id default --project-id my-project
```

記憶を `feedback` / `profile` / `reference` に分類して返す（archive 済み = forget 済みの記憶は含まれない）。大きな作業に着手する前にまず実行する。

### search（キーワード検索）

```bash
python3 ~/.agents/memory/memory.py search --query 'vim' --project-id my-project
```

archive 済み（forget 済み）を除いた現行の記憶のみが対象。`--memory-type` / `--scope` / `--entity-id` / `--limit` で絞り込み可能（`search --help` 参照）。

### history（過去セッション横断検索）

```bash
python3 ~/.agents/memory/memory.py history --query 'vim'
```

`memories` / `sessions` / `events` を横断して過去の経緯を掘る。`--no-memories` / `--no-sessions` / `--no-events` で対象を絞れる（`history --help` 参照）。

**注意**: `--project-id` を付けると `scope=global` の記憶（`project_id` が NULL）は除外されるため一致しない。global スコープの記憶を含めて検索したい場合は `--project-id` を省略する。

## 削除（forget）

```bash
python3 ~/.agents/memory/memory.py forget --memory-id global/preferred-language-runtime --reason "ユーザー環境が変わったため"
```

`--memory-id` と `--reason`（いずれも必須）を指定する。`--memory-id` は `mem_xxxx` のような不透明な ID ではなく、`search`/`get-context` が返す `id`（例: `global/preferred-language-runtime`、プロジェクトスコープなら `projects/<project_id>/<slug>`）をそのまま渡す。`forget` は記憶ファイルを削除するのではなく `memory/archive/` 配下へ移動するだけなので、`search`/`get-context`/`history` からは見えなくなるが、Vault 上には残り続ける。

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
