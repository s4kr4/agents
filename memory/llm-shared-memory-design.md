# LLM Shared Memory Design

## 目的

Codex、Claude Code など複数の LLM 実行環境から参照できる共有記憶領域を作る。

満たしたい要件:

- セッションをまたいで記憶を保持する
- 利用履歴から使用者に関する情報を抽出し、次回応答で参照できる
- 複数クライアントから同じ記憶を読む
- 記憶の根拠、更新履歴、削除を扱える
- 誤記憶や過剰推論を抑制する

## 設計方針

共有メモリは 1 つの巨大なプロフィールではなく、次の 3 層に分ける。

1. `event`
   セッションや発話などの生データ。追記中心。
2. `observation`
   event から抽出された候補情報。まだ確定ではない。
3. `memory`
   継続参照に値すると判定された記憶。根拠と信頼度を持つ。

この分離で、`会話履歴` と `LLM が参照すべき安定記憶` を混同しない。

## 記憶の分類

### 1. Semantic memory

比較的長く有効な事実。

- 使用者の好み
- よく使う言語、OS、エディタ
- プロジェクト固有のルール
- 回答スタイルの好み

### 2. Episodic memory

特定の作業や時系列イベント。

- 直近の作業内容
- 失敗した手順
- 未完了タスク
- 前回の会話で合意した設計判断

### 3. Procedural memory

振る舞いのためのルール。

- 回答は日本語
- テスト前に特定コマンドを実行する
- このリポジトリでは `apply_patch` を使う

Procedural memory は通常の user profile より優先度を高く扱う。

## 推奨アーキテクチャ

### 結論

初期構成は `SQLite + CLI ツール + 定期的な要約/抽出ジョブ` を推奨する。

理由:

- 単一ユーザー用途では運用が軽い
- ローカルファイル 1 つでバックアップしやすい
- Codex / Claude Code のようなローカル実行系と相性がよい
- 常駐サーバーなしで始められる
- まずは FTS とメタデータ検索で十分に始められる

将来的に複数端末同期や高頻度アクセスが必要なら `PostgreSQL + pgvector` に移行する。

## コンポーネント

### 1. Memory Store

記憶本体の保存先。

- 初期推奨: SQLite
- 拡張案: PostgreSQL

### 2. Memory CLI

各 LLM クライアントが共通で呼ぶインターフェース。

提供コマンド例:

- `memory append-event`
- `memory extract`
- `memory consolidate`
- `memory search`
- `memory get-context`
- `memory forget`

CLI は標準入力または引数で JSON を受け取り、標準出力へ JSON を返す。

### 3. Optional API Layer

将来必要になれば、CLI の内部ロジックを再利用して HTTP API や MCP server を載せる。

### 4. Extractor

会話履歴や使用ログから observation を生成する。

- ルールベース: セッション終了時に hook から `--extract` フラグで実行（キーワードマッチ）
- LLM ベース: Claude Code のスキル `/memory-extract` でバッチ手動実行。Claude Code 自身がセッション要約を分析し、`write-memory` コマンドで書き込む

### 5. Consolidator

observation から安定記憶を更新する。

- 重複統合
- 信頼度更新
- 古い記憶の失効
- 矛盾検知

## データモデル

### `sessions`

セッション単位のメタデータ。

| column | type | note |
|---|---|---|
| id | text | UUID |
| client | text | `codex`, `claude-code` など |
| user_id | text | 単一ユーザーでも明示 |
| project_id | text nullable | プロジェクト単位の文脈 |
| started_at | datetime | 開始 |
| ended_at | datetime nullable | 終了 |
| summary | text nullable | セッション要約 |

### `events`

生ログ。append-only を基本とする。

| column | type | note |
|---|---|---|
| id | text | UUID |
| session_id | text | FK |
| role | text | `user`, `assistant`, `system`, `tool` |
| kind | text | `message`, `command`, `file_change`, `summary` など |
| content | text | 本文または JSON |
| created_at | datetime | 発生時刻 |
| importance | real | 後段抽出のヒント |

### `observations`

抽出された候補事実。

| column | type | note |
|---|---|---|
| id | text | UUID |
| source_event_id | text | 根拠 |
| entity_type | text | `user`, `project`, `environment`, `agent` |
| entity_id | text | 例: `user:default` |
| attribute | text | 例: `preferred_language` |
| value_json | text | 構造化値 |
| confidence | real | 0.0-1.0 |
| scope | text | `global`, `project`, `client` |
| observed_at | datetime | 抽出日時 |
| extractor_version | text | 抽出ロジックの版 |

### `memories`

参照対象となる安定記憶。

| column | type | note |
|---|---|---|
| id | text | UUID |
| memory_type | text | `semantic`, `episodic`, `procedural` |
| entity_type | text | `user`, `project`, `environment`, `agent` |
| entity_id | text | 対象 |
| key | text | 正規化済みキー |
| value_json | text | 値 |
| summary | text | LLM に渡す短い要約 |
| confidence | real | 0.0-1.0 |
| salience | real | 想起優先度 |
| status | text | `active`, `superseded`, `deleted` |
| valid_from | datetime | 有効開始 |
| valid_until | datetime nullable | 有効終了 |
| created_at | datetime | 作成日時 |
| updated_at | datetime | 更新日時 |

### `memory_sources`

1 つの memory の根拠は複数 event / observation にまたがる。

| column | type | note |
|---|---|---|
| memory_id | text | FK |
| observation_id | text | FK |
| weight | real | 根拠寄与度 |

### `retrieval_logs`

何を参照して応答したかを残す。

| column | type | note |
|---|---|---|
| id | text | UUID |
| session_id | text | FK |
| query | text | 検索要求 |
| returned_memory_ids | text | JSON array |
| created_at | datetime | 実行時刻 |

### `deletions`

忘却や禁止を管理する。

| column | type | note |
|---|---|---|
| id | text | UUID |
| target_type | text | `event`, `observation`, `memory` |
| target_id | text | 対象 |
| reason | text | 削除理由 |
| created_at | datetime | 削除日時 |

## 属性モデリング

`value_json` はスキーマ固定しすぎない。初期は JSON を許容し、よく使う項目だけ正規化する。

例:

```json
{
  "key": "preferred_language",
  "value": "ja",
  "source": "explicit_user_statement",
  "evidence": "応答は日本語で行う",
  "scope": "global"
}
```

使用者情報は次のようなカテゴリに分けると扱いやすい。

- `preferences`
- `identity`
- `environment`
- `projects`
- `constraints`
- `habits`

## 記憶更新フロー

### Ingest

1. セッション開始時に `sessions` を作成
2. 発話やツール実行を `events` に追記
3. セッション終了時に要約を追加

DB に書き込めない環境では、`queue-session` でファイルキュー（JSONL）にフォールバックし、後で `flush-queue` で DB に反映する。

### Extract

2段階の抽出を行う。

**ルールベース（hook 自動実行）:**
セッション終了時に `--extract` フラグで呼ばれ、キーワードマッチで明示的な宣言（言語設定、OS、エディタ等）を抽出する。

**LLM ベース（手動バッチ実行）:**
`/memory-extract` スキルで Claude Code 自身がセッション要約を分析し、長期的に有効な意味記憶を判断して `write-memory` コマンドで書き込む。一時的な作業内容は抽出対象外。

### Consolidate

1. 同じ key の既存 memory を探索
2. 値が一致すれば confidence を上げる
3. 値が矛盾すれば新旧を並立させ、古いものを `superseded` にする

### Retrieve

応答時は以下の順で絞る。

1. procedural memory
2. 現在の project に紐づく semantic memory
3. user global profile
4. 最近の episodic memory

無制限に渡さず、上位 5-20 件程度に制限する。

## 検索戦略

初期はベクトル検索なしでもよい。

- 厳密 key 検索
- entity / scope / project フィルタ
- SQLite FTS5 による全文検索
- `salience * recency * confidence` によるスコアリング

ベクトル検索が必要になる条件:

- 記憶件数が数万を超える
- 同義表現が多い
- 自由文から profile を引きたい

その場合の候補:

- SQLite 継続: `sqlite-vec`
- DB 移行: `pgvector`

## クライアント統合方法

### 推奨

各 LLM 環境から同じ CLI を呼ぶ。

利点:

- 実装とデバッグが単純
- 常駐プロセスが不要
- シェルフックや終了時処理に組み込みやすい
- 将来バックエンドを SQLite から PostgreSQL に変えても CLI 契約を維持しやすい

注意点:

- 各クライアントでコマンド呼び出しの組み込みは必要
- 入力検証や認可を中央集約しにくい

### 将来拡張

複数端末共有、リモートアクセス、統一認可が必要になったら API/MCP を追加する。

## 必要技術

### 最小構成

- 言語: Python または TypeScript
- DB: SQLite
- CLI: `argparse` / `typer` / `click` か Node.js の `commander`
- スキーマ管理: SQLAlchemy/Alembic か drizzle/kysely 相当
- 全文検索: SQLite FTS5
- 定期処理: cron / systemd timer / task runner

### 拡張構成

- DB: PostgreSQL + pgvector
- API: FastAPI / Hono / Express
- MCP: 必要時に追加
- 認証: API key またはローカルソケット
- 監査: structured logging

## 技術選定の比較

### SQLite

向いている条件:

- 単一ユーザー
- ローカル中心
- まず動かしたい

長所:

- 導入が最も軽い
- バックアップしやすい
- トランザクションが堅い

短所:

- 複数端末同期は別途必要
- 高並行書き込みには弱い

### PostgreSQL

向いている条件:

- 複数端末から常時参照
- モバイルや外部サービスも繋ぐ
- ベクトル検索や権限制御を強めたい

長所:

- 拡張性が高い
- pgvector を使いやすい
- 同時アクセスに強い

短所:

- 運用コストが増える

### JSON / Markdown ファイル直置き

向いている条件:

- 試作だけ
- 人間可読性を最優先

短所:

- 検索、整合性、同時更新、削除履歴に弱い
- 中期運用に耐えづらい

## 安全策

### 1. 推測を書き込まない

「多分こういう人だ」は memory にしない。明示発言か反復行動のみ。

### 2. 根拠を必須にする

memory は最低 1 つ以上の source を持つ。

### 3. スコープを持つ

- global
- project
- client
- temporary

これがないと、別プロジェクトの癖を誤って一般化しやすい。

### 4. 忘却を設計に含める

- 削除
- 失効
- 上書き
- 参照禁止

### 5. 個人情報の隔離

センシティブ情報は専用フラグを持たせ、既定では応答に渡さない。

## 推奨する初期 CLI

### `memory append-event`

入力:

- session_id
- role
- kind
- content

### `memory extract`

入力:

- session_id

出力:

- 生成した observation 一覧

### `memory consolidate`

入力:

- user_id
- project_id optional

### `memory search`

入力:

- query
- user_id
- project_id optional
- memory_types
- limit

### `memory get-context`

応答生成前に使う。

出力例:

- procedural memory
- active project memory
- user preference summary
- recent episodic memory

### `memory write-memory`

抽出された知識を直接書き込む。observations を経由して consolidate まで一括実行。

入力:

- session_id
- memory_type (semantic / episodic / procedural)
- key
- summary
- confidence
- scope (global / project)
- project_id (optional)

### `memory list-unextracted`

LLM 抽出が未実行のセッション一覧を返す。

入力:

- limit (default: 10)

### `memory mark-extracted`

セッションを LLM 抽出済みとしてマークする。

入力:

- session_id

### `memory cleanup`

不要な superseded memories の重複削除と recent_summary データの削除を行う。

### `memory queue-session`

DB 書き込み不可時にセッション情報を JSONL ファイルキューに保存する。

入力:

- session_id
- client
- user_id
- project_id
- user_content
- assistant_content
- summary

### `memory flush-queue`

キューに溜まった JSONL ファイルを DB に書き込む。

## CLI 契約

すべてのコマンドは次の原則に揃える。

- 成功時は JSON を stdout に出す
- エラー時は stderr に要因を出し、非 0 で終了する
- `--json` を既定にするか、少なくとも機械可読出力を常に選べるようにする
- `--db` で DB パスを上書き可能にする
- `--user-id`, `--project-id`, `--client` を明示的に渡せるようにする

例:

```bash
memory append-event \
  --db ~/.local/share/llm-memory/memory.db \
  --session-id s_123 \
  --role user \
  --kind message \
  --content '{"text":"応答は日本語で"}'
```

```bash
memory get-context \
  --db ~/.local/share/llm-memory/memory.db \
  --user-id default \
  --project-id agents \
  --format json
```

出力例:

```json
{
  "procedural": [
    {
      "key": "response_language",
      "summary": "応答は日本語で行う",
      "confidence": 1.0
    }
  ],
  "semantic": [],
  "episodic": []
}
```

## 実装順

1. SQLite スキーマ作成
2. `memory append-event`, `memory search`, `memory get-context` を先に作る
3. 抽出は最初はルールベースで開始する
4. `memory extract`, `memory consolidate` を追加する
5. 必要になってから embedding を足す
6. 複数端末同期や高頻度アクセスが必要になったら API または PostgreSQL に移行する

## この用途での現実的な結論

この要件では、最初からベクトル DB 中心にするより、`根拠付きの構造化 memory store` を先に作る方がよい。

特に使用者プロフィールや応答方針は、曖昧な意味検索よりも次の性質が重要:

- 誰についての記憶か
- どのプロジェクトで有効か
- 根拠は何か
- 今も有効か

そのため初期推奨は以下。

- 保存: SQLite
- 参照口: CLI
- 検索: key + FTS5
- 記憶単位: event / observation / memory の 3 層
- 同期: 必要になるまでローカル運用

将来、件数や接続元が増えたら `API/MCP` や `PostgreSQL + pgvector` に移す。
