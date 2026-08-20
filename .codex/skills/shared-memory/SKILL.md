---
name: shared-memory
description: Claude Code / Codex 横断で共有するローカルメモリ（ファイルベース）への保存と読み出し。Use when user says "覚えておいて", "記録しておいて", "前回の作業", "共有メモリ", or "/shared-memory". Also use proactively: プロジェクト横断で再利用されうる判断・実装/検証フロー・恒常的な好み・環境制約を扱う可能性がある作業では、開始時から使い、完了前に候補を評価する。明らかに自己完結した軽微作業は省略可。
---

# Shared Memory

Claude Code と Codex の間で共有するローカル記憶を、作業の文脈に反映しながら、自分で読み書きするためのスキル。毎ターン自動で書き込む仕組みは廃止されており、保存・読み出しはこのスキル経由でのみ行う。

## 標準フロー

共有メモリは、明示的な保存依頼だけでなく、プロジェクトをまたいで再利用できる知見を扱う標準フローとして積極的に使う。ただし、会話全文や作業ログを蓄積する場所にはしない。

### 1. 作業開始時: 文脈を取得する

作業開始時に、過去の経緯・ユーザーの好み・既知の制約・以前の判断が影響し得るかを確認する。影響し得る作業、および大きな作業では、最初に `get-context` を実行する。得られた `feedback` / `profile` / `reference` を初期方針へ反映する。必要に応じて `search` または `history` で補足する。文脈が影響しないことが明らかな軽微・自己完結の作業だけは省略できる。

### 2. 対話・作業中: 知見候補を三層に分類する

ユーザーとの対話、調査、実装、検証から得た知見のうち、将来の作業で判断を変えうるものだけを軽量な候補として収集する。候補は直ちに全文保存せず、当該タスクの作業計画または引き継ぎメモに短い箇条書きとして保持する。タスクを終える前に必ず候補の有無を確認し、次の三層に分類する。

| 層 | 対象 | 扱い |
| --- | --- | --- |
| 作業記録 | タスク固有の経緯、未解決事項、一時的な事実 | 当該プロジェクト・セッションの短期記録に留め、共有メモリには保存しない。 |
| 共有知見 | プロジェクト横断で有効な判断、ユーザーの恒常的な好み、環境上の制約、繰り返し参照するプロジェクト固有の決定事項 | 十分に確認できた要約知識だけを、適切な global または project scope の共有メモリ候補にする。 |
| 規約化知見 | 反復的で、具体的な手順・ルールとして固定すべき知見 | `AGENTS.md`、rule、skill への昇格候補にする。 |

### 3. 作業完了時: 統合して保存・昇格判断する

完了時に候補を見直し、既存の共有メモリを `get-context` または `search` で確認して重複・矛盾を統合する。保存する場合は、将来の判断に使える短い要約に圧縮して `write-memory` を使う。

規約化知見は、次の **すべて** を満たす場合だけ、既存の `AGENTS.md`・rule・skill との重複を確認したうえで昇格を提案する。更新する場合は、対象プロジェクトの規約とユーザー承認要件に従う。

1. 少なくとも複数回、異なる作業で再利用された。
2. 曖昧な好みではなく、具体的な行動に落とし込める。
3. 守らない場合に、品質・安全性・効率への明確な悪影響がある。
4. 既存の skill、rule、`AGENTS.md` と重複しない。
5. プロジェクト固有の規約か、横断的な規約かを判定できる。

## 基盤情報

ファイルベースの 2 層構成（詳細は `/memory` スキル参照）。

- Vault（memories・Syncthing 同期対象）: `$LLM_MEMORY_VAULT/memory/`（未設定時は `~/.agents/memory/vault/` にフォールバック）
- local（sessions/events/observations・同期対象外）: `$LLM_MEMORY_LOCAL_DIR`（既定 `~/.agents/memory/local/`）
- CLI: `~/.agents/memory/run-python.sh ~/.agents/memory/memory.py`

## Vault の配置原則

- `memory/` 直下に置く Markdown ファイルは生成された `_index.md` のみとする。
- 個別の記憶は scope に対応する分類ディレクトリ（例: `memory/global/`、`memory/projects/<project-slug>/`）内へ保存する。
- 記憶ファイルを Vault 直下へ手作業で作成しない。CLI の `write-memory` を使い、分類・索引更新を任せる。

## 保存フロー（write-memory）

`write-memory` は `session_id` に紐づく event を作ってから observation → memory を作る。会話中のセッション ID を把握していない場合は、先に `start-session` で仮のセッションを作ってから書き込む。

```bash
# 1. セッションがなければ作成（初回のみ）
~/.agents/memory/run-python.sh ~/.agents/memory/memory.py start-session \
  --client claude-code --user-id default --project-id my-project \
  --session-id manual-$(date +%s)

# 2. 記憶を書き込む
~/.agents/memory/run-python.sh ~/.agents/memory/memory.py write-memory \
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
- `--confidence`: デフォルト 0.8。明示的発言や検証済みの判断には 0.8〜1.0 を指定する。未検証の推測は候補または作業記録に留め、`write-memory` には確認済みの知見だけを渡す。
- `--scope`: `global`（全プロジェクト共通、デフォルト）/ `project`（特定プロジェクト、`--project-id` も必須指定。`--project-id` を省略するとエラーになる）
- `--entity-type` / `--entity-id`: デフォルト `user` / `default`。プロジェクト固有の記憶なら `project` / project 名

## 読み出しフロー

### get-context（作業開始時の文脈取得）

```bash
~/.agents/memory/run-python.sh ~/.agents/memory/memory.py get-context --user-id default --project-id my-project
```

記憶を `feedback` / `profile` / `reference` に分類して返す（archive 済み = forget 済みの記憶は含まれない）。大きな作業に着手する前にまず実行する。

### search（キーワード検索）

```bash
~/.agents/memory/run-python.sh ~/.agents/memory/memory.py search --query 'vim' --project-id my-project
```

archive 済み（forget 済み）を除いた現行の記憶のみが対象。`--memory-type` / `--scope` / `--entity-id` / `--limit` で絞り込み可能（`search --help` 参照）。

### history（過去セッション横断検索）

```bash
~/.agents/memory/run-python.sh ~/.agents/memory/memory.py history --query 'vim'
```

`memories` / `sessions` / `events` を横断して過去の経緯を掘る。`--no-memories` / `--no-sessions` / `--no-events` で対象を絞れる（`history --help` 参照）。

**注意**: `--project-id` を付けると、対象プロジェクトの記憶と global スコープの記憶を検索する。global スコープだけに絞る場合は `--scope global` を指定する。

## 削除（forget）

```bash
~/.agents/memory/run-python.sh ~/.agents/memory/memory.py forget --memory-id global/preferred-language-runtime --reason "ユーザー環境が変わったため"
```

`--memory-id` と `--reason`（いずれも必須）を指定する。`--memory-id` は `mem_xxxx` のような不透明な ID ではなく、`search`/`get-context` が返す `id`（例: `global/preferred-language-runtime`、プロジェクトスコープなら `projects/<project_id>/<slug>`）をそのまま渡す。`forget` は記憶ファイルを削除するのではなく `memory/archive/` 配下へ移動するだけなので、`search`/`get-context`/`history` からは見えなくなるが、Vault 上には残り続ける。

## 保存基準

保存するのは「長期的に有効」かつ「ツール横断で有用」で、十分に確認された要約知識のみ。

**保存する**:
- ユーザーの技術的な好み（エディタ、言語、フレームワーク等）
- 開発環境・ツールの設定
- 繰り返し参照するプロジェクト固有の決定事項

**保存しない**:
- 会話全文、逐語的な作業ログ、会話限りの一時的な情報（そのセッションだけで完結する作業内容）
- 未検証の推測、仮説、推論途中の判断
- リポジトリに既に記録されている事実（コード構造・git 履歴・CLAUDE.md 記載事項）
- Claude Code 専用の文脈（標準メモリ機構の担当領域であり、ツール横断の共有メモリで扱う必要はない）

## 関連スキル

- 基盤自体の不具合調査・hook やスキーマの確認: `/memory`
- セッション履歴からの一括抽出: `/memory-extract`
