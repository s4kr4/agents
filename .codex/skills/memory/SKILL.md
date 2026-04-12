---
name: memory
description: 複数の LLM 実行環境で共有するローカルメモリ基盤を扱うときに使う。memory/memory.py、SQLite スキーマ、hook、起動ラッパーを確認し、現行コンテキスト取得、履歴横断検索、保存不具合の切り分けを、event / observation / memory の役割を保ちながら進める。
---

# Memory

このスキルは、Codex や Claude Code など複数クライアントから同じローカル記憶を読むための共有メモリ基盤を扱うときに使う。

目的は次の 3 つ。

- セッションをまたいで参照したい安定情報を保持する
- 過去の作業経緯や判断理由を、必要なときだけ掘り返せるようにする
- 生ログと参照用記憶を混同せず、誤記憶や過剰一般化を抑える

## 先に見るファイル

- `memory/memory.py`
- `memory/llm-shared-memory-schema.sql`
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
  次回以降の応答で参照する安定記憶。confidence、salience、source を持つ。

この分離を崩さない。

- 過去の会話全文をそのまま profile 化しない
- 一時的な作業ログと長期記憶を同じ扱いにしない
- 根拠のない推測を memory にしない

## `memories.status` の意味

- `active`
  現在有効な現行値
- `superseded`
  新しい記憶に置き換えられた履歴
- `deleted`
  明示的に忘却された記憶

`active` は「今の応答に使う値」、`superseded` は「過去の経緯を追うための履歴」として扱う。

## 使い分け

- 応答前に注入する現行コンテキストが欲しい:
  `get-context`
- 現在有効な記憶だけ検索したい:
  `search`
- 過去セッションや superseded を含めて経緯を掘りたい:
  `history`
- 保存フローの確認や手動投入をしたい:
  `start-session` / `append-event` / `end-session`
- 抽出や統合ロジックを確認したい:
  `extract` / `consolidate`
- 明示的に忘却したい:
  `forget`
- DB に書き込めない環境でセッションを保存したい:
  `queue-session`
- キューに溜まったセッションを DB に反映したい:
  `flush-queue`
- セッションから意味記憶を抽出したい:
  `/memory-extract` スキル（`list-unextracted` / `write-memory` / `mark-extracted`）
- 不要な superseded データを整理したい:
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
python3 memory/memory.py flush-queue --db memory/memory.db
```

## 調査の進め方

1. まず `get-context` か `search` で `active` な現行記憶を確認する。
2. 欲しい情報が現行記憶にないなら `history` へ切り替える。
3. `history` では `memories` だけでなく `sessions` と `events` も見る。
4. 件数差があるときは、`status` と `scope` の内訳を確認する。
5. 保存が弱いときは、hook やラッパーがどの event を投入しているか確認する。
6. 抽出が弱いときは、`observations` に何が作られているかを見る。

## よくある誤解

- `search` は基本的に現行値向けで、履歴探索には向かない。
- `history` は経緯探索用で、今の応答にそのまま大量注入する用途ではない。
- テーブル件数が多くても、その大半が `superseded` なら `search` では見えない。
- 自動保存があることと、自動参照が統合されていることは別。
- `recent_summary` だけに依存すると、検索精度は summary の品質に引っ張られる。

## 保存フロー

- Claude Code
  `memory/hook-stop-memory.sh` が Stop hook として実行される。
  1. DB に直接書き込みを試みる（`start-session` → `append-event` → `end-session`）
  2. 失敗した場合、`queue-session` でファイルキューにフォールバックする
- Codex
  `memory/codex-memory-run.sh` と各ラッパーから、開始・追記・終了を呼ぶ。

必要なら `LLM_MEMORY_DB` で DB パスを切り替える。

### ファイルキュー

DB に書き込めない環境（別プロジェクトから Claude Code を起動した場合など）で使われるフォールバック機構。

- キューの実体はセッション情報を格納した JSONL ファイル
- 保存先: `~/.cache/llm-memory/queue/`（`LLM_MEMORY_QUEUE_DIR` で変更可）
- `flush-queue` で溜まったキューを DB に一括反映する
- キューの保存は SQLite に一切触れないため、どの環境でも失敗しない

## 出力の原則

- 現行記憶か、履歴かを先に明示する。
- `active / superseded / deleted` を混ぜて説明しない。
- 経緯を答えるときは、該当 memory のみで断定せず、関連する session / event を添える。
- 「件数はあるのに取れない」ときは、まず `status`、`scope`、`query` 一致条件を説明する。
- 必要以上に過去ログ全文を出さず、根拠のある抜粋と要約に留める。
# test change
