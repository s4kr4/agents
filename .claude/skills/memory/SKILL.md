---
name: memory
description: 複数の LLM 実行環境で共有するローカルメモリ基盤を扱うときに使う。memory.py、Vault/local のファイルストア、hook、起動ラッパーの確認と、コンテキスト取得・履歴検索・保存不具合の切り分けを行う。Use when user says "メモリの設定", "記憶が保存されない", "memory.py", "メモリDB", or "/memory".
---

# Memory

このスキルは、Codex や Claude Code など複数クライアントから同じローカル記憶を読むための共有メモリ基盤を扱うときに使う。

目的は次の 3 つ。

- セッションをまたいで参照したい安定情報を保持する
- 過去の作業経緯や判断理由を、必要なときだけ掘り返せるようにする
- 生ログと参照用記憶を混同せず、誤記憶や過剰一般化を抑える

## ストレージ構成

ファイルベースの 2 層構成。

- **Vault**（`memories`、Syncthing 同期対象）: `$LLM_MEMORY_VAULT/memory/`。直下の Markdown は `_index.md` のみで、各記憶は `global/`・`projects/`・`clients/`・`temporary/` の分類ディレクトリ内に置く（frontmatter 付き Markdown、1 記憶 = 1 ファイル）。未設定時は `~/.agents/memory/vault/` にフォールバックし、stderr に警告を出す。
- **local**（`sessions`/`events`/`observations`/監査ログ、同期対象外）: `$LLM_MEMORY_LOCAL_DIR`（既定 `~/.agents/memory/local/`）。

`--db` フラグは deprecated no-op（受理するが無視）。既存ラッパーとの互換用に残している。

## 先に見るファイル

- `memory/memory.py`
- `memory/markdown_store.py`（Vault: memories の読み書き）
- `memory/local_store.py`（local: sessions/events/observations/監査ログ）
- `memory/migrate_sqlite_to_markdown.py`（旧 SQLite からの一括移行、dry-run 既定）
- `memory/hook-stop-memory.sh`
- `memory/codex-memory-start.sh`
- `memory/codex-memory-log.sh`
- `memory/codex-memory-stop.sh`
- `memory/codex-memory-run.sh`

## モデルの原則

共有メモリは 3 層で扱う。

- `events`
  発話、コマンド、summary などの生ログ。append-only。
- `observations`
  event から抽出した候補情報。まだ確定事実ではない。
- `memories`
  次回以降の応答で参照する安定記憶。Vault 上は 1 論理キー = 1 Markdown
  ファイルで、frontmatter は `type`/`created`/`updated` のみを持つ
  （旧来の confidence/salience/source は廃止済み。詳細は
  `memory/markdown_store.py` のモジュール docstring を参照）。

この分離を崩さない。

- 過去の会話全文をそのまま profile 化しない
- 一時的な作業ログと長期記憶を同じ扱いにしない
- 根拠のない推測を memory にしない

## 記憶の現行/履歴/archive の扱い

`memories.status`（`active`/`superseded`/`deleted`）という概念は廃止済み。
現在のモデルは次の通り:

- **現行値**
  ファイル本文の冒頭（summary）が「今の応答に使う値」。値が変わるたびに
  同じファイルの内容が上書きされる（別ファイルは作られない）。
- **履歴**
  同じファイル内の `## 変更履歴` セクションに、過去の値への変更が
  時系列で追記される（旧来の `superseded` 行に相当）。
- **archive**
  明示的に `forget` された記憶は `memory/archive/` 配下へ物理的に
  移動される（旧来の `status: deleted` フラグに相当）。削除ではなく
  移動なので Vault 上には残るが、`search`/`get-context`/`history`
  などの読み出し系コマンドからは常に除外される。

## 使い分け

- 応答前に注入する現行コンテキストが欲しい:
  `get-context`
- 現行の記憶（archive されていないもの）だけ検索したい:
  `search`
- 過去セッションや変更履歴を含めて経緯を掘りたい:
  `history`
- 保存フローの確認や手動投入をしたい:
  `start-session` / `append-event` / `end-session`
- 抽出や統合ロジックを確認したい:
  `extract` / `consolidate`
- 明示的に忘却（archive へ移動）したい:
  `forget`
- ファイルストアに書き込めない環境でセッションを保存したい:
  `queue-session`
- キューに溜まったセッションをファイルストアに反映したい:
  `flush-queue`
- セッションから意味記憶を抽出したい:
  `/memory-extract` スキル（`list-unextracted` / `write-memory` / `mark-extracted`）
- 不要な `recent_summary` データを整理したい:
  `cleanup`

## 基本コマンド

DB 初期化:

```bash
python3 memory/memory.py init-db
```

現行コンテキスト取得:

```bash
python3 memory/memory.py get-context --user-id default --project-id my-project
```

現行記憶検索:

```bash
python3 memory/memory.py search --project-id my-project --query 'keyword'
```

履歴横断検索:

```bash
python3 memory/memory.py history --project-id my-project --query 'decision background'
python3 memory/memory.py history --project-id my-project --query 'keyword' --no-events
python3 memory/memory.py history --project-id my-project --query 'keyword' --no-memories --limit 5
```

手動で保存フローを動かす例:

```bash
python3 memory/memory.py start-session --client codex --user-id default --project-id my-project --session-id demo
python3 memory/memory.py append-event --session-id demo --client codex --user-id default --project-id my-project --role user --kind message --content 'Respond in Japanese.'
python3 memory/memory.py end-session --session-id demo --append-summary-event --extract --consolidate
```

キューベースの保存（DB 書き込み不可時）:

```bash
python3 memory/memory.py queue-session \
  --session-id demo \
  --client claude-code \
  --user-id default \
  --project-id my-project \
  --user-content 'ユーザーの発言' \
  --assistant-content 'アシスタントの応答' \
  --summary 'セッション要約'
```

キューの flush:

```bash
python3 memory/memory.py flush-queue
```

## 調査の進め方

1. まず `get-context` か `search` で現行記憶（archive されていない記憶）を確認する。
2. 欲しい情報が現行記憶にないなら `history` へ切り替える（`## 変更履歴` に残る過去の値も見る）。
3. `history` では `memories` だけでなく `sessions` と `events` も見る。
4. 件数差があるときは、`scope`（global/project/client/temporary）の内訳と archive の有無を確認する。
5. 保存が弱いときは、hook やラッパーがどの event を投入しているか確認する。
6. 抽出が弱いときは、`observations` に何が作られているかを見る。

## よくある誤解

- `search` は基本的に現行値向けで、履歴探索には向かない。
- `history` は経緯探索用で、今の応答にそのまま大量注入する用途ではない。
- 変更履歴（`## 変更履歴`）が長くても、`search`/`get-context` には現行の summary しか出てこない。
- 自動保存があることと、自動参照が統合されていることは別。
- `recent_summary` だけに依存すると、検索精度は summary の品質に引っ張られる。

## 保存フロー

- Claude Code
  現在 Stop フックは配線されておらず、読み書きは `/shared-memory` スキル経由でモデルが必要と判断したときに行う（`memory/hook-stop-memory.sh` は未配線のまま保持している）。
- Codex
  `memory/codex-memory-run.sh` と各ラッパーから、開始・追記・終了を呼ぶ。

必要なら `LLM_MEMORY_VAULT` / `LLM_MEMORY_LOCAL_DIR` で保存先を切り替える。

### ファイルキュー

local ディレクトリに書き込めない環境（別プロジェクトから Claude Code を起動した場合など）で使われるフォールバック機構。

- キューの実体はセッション情報を格納した JSONL ファイル
- 保存先: `~/.cache/llm-memory/queue/`（`LLM_MEMORY_QUEUE_DIR` で変更可。Vault/local ディレクトリとは独立）
- `flush-queue` で溜まったキューを Vault/local に一括反映する
- キューの保存自体はどの環境でも失敗しない（Vault/local への書き込みとは独立したファイル操作のみ）

## 出力の原則

- 現行記憶か、履歴（`## 変更履歴`）か、archive（forget 済み）かを先に明示する。
- 現行値と履歴を混ぜて説明しない。
- 経緯を答えるときは、該当 memory のみで断定せず、関連する session / event を添える。
- 「件数はあるのに取れない」ときは、まず `scope`、`query` 一致条件、archive されていないかを説明する。
- 必要以上に過去ログ全文を出さず、根拠のある抜粋と要約に留める。
