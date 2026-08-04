# LLM Shared Memory Design

> **実装状況（現在）**: 以下の設計は当初 SQLite 中心で書かれたが、実装は
> `SQLite + CLI` から `Vault Markdown（memories 層）+ ローカルファイル
> （sessions/events/observations 層）` へ移行済み。移行の背景・現在のファイル
> レイアウトは「実装後の構成（現状）」章を参照。歴史的経緯として残す設計比較
> （SQLite / PostgreSQL / JSON・Markdown 直置き）はそのまま残し、末尾に補足を
> 追記している。さらにその後、Markdown 化した frontmatter 自体が SQLite の
> テーブル列をほぼ 1:1 で持ち込んだ構造になっていた問題を見直し、
> `type`/`created`/`updated` の 3 フィールドのみへ削減した。詳細は
> 「frontmatter の最小化（RDB 構造からの脱却）」章を参照。

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

## 実装後の構成（現状）

上記のデータモデルは概念設計として現在も有効だが、実装は SQLite の単一
テーブル群ではなく、2 つのファイルベースのストアに分割されている。

### レイヤーとストレージの対応

| レイヤー | 実装 | 保存先 | 同期 |
|---|---|---|---|
| `memories` | `MarkdownMemoryStore`（`memory/markdown_store.py`） | `$LLM_MEMORY_VAULT/memory/*.md` | Syncthing で複数端末に同期 |
| `sessions` / `events` / `observations` | `LocalPipelineStore`（`memory/local_store.py`） | `$LLM_MEMORY_LOCAL_DIR` 配下（JSON / JSONL） | 同期しない（機体ローカル） |

`memories` は継続参照する安定記憶であり件数も少ないため、人間可読性と
Syncthing 同期を優先して Markdown 化した。`sessions`/`events`/`observations`
は高頻度追記の生ログであり、同期対象にすると Syncthing のトラフィックと
コンフリクトリスクが増えるだけなので、従来どおり機体ローカルに留めている。

### 1 論理キー = 1 ファイル（生きたドキュメントとしてのメモリ）

初期の Markdown 化では、SQLite の「1 レコード = 1 行」の発想をそのまま
持ち込み、値が更新されるたびに旧レコードを別ファイル
（`<canonical-slug>-superseded-<marker>.md`）へ退避していた。実データでは
`primary-os.md` の隣に `primary-os-superseded-<marker1>.md`、
`primary-os-superseded-<marker2>.md` ... のような同一トピックの断片ファイル
が量産され、Obsidian で見たときに「今何が最新か」が一目でわからなくなる
問題があった。

これを改め、**1 つの論理キー（entity_type + entity_id + key + scope +
project_id）に対応するファイルは常に 1 つだけ**という制約に変更した。
Obsidian のようなノートアプリでは、1 ファイルは「あるトピックについて今
分かっている最新の理解」を表すべきで、過去の変遷はそのファイル内の履歴と
して内包されるのが自然、という考え方への転換である。

- **YAML frontmatter は `type`/`created`/`updated` の 3 フィールドのみ**を
  保持する（詳細・経緯は「frontmatter の最小化（RDB 構造からの脱却）」章を
  参照）。`created` はそのトピックの記憶が最初に作られた日付のまま更新せず、
  `updated` は最新の変更日で上書きする。
- **内容が変わっても新しいファイルは作らない。** 代わりに本文へ
  `## 変更履歴` セクションを設け、変更前の内容を 1 行（`<変更日>: <変更前>
  → <変更後> に変更` 形式）追記してから、本文とその他 frontmatter を新しい
  内容で上書きする
  （`MarkdownMemoryStore.upsert_from_observation` / `render_history_line`）。
- upsert 判定は「本文の要約が同一なら `updated` のみ更新、異なれば旧要約を
  履歴に追記して本文を更新」というシンプルな文字列比較になっている
  （旧来の `confidence` 加点のような数値スコアリングは行わない -- 詳細は
  次章）。
- `forget`（明示的な削除指示）は `memory/archive/` 配下へ**物理的にファイルを
  移動する**（削除ではなく `os.replace`）。`status: deleted` フラグは廃止した。
  詳細は「frontmatter の最小化（RDB 構造からの脱却）」章を参照。

### Vault 内のディレクトリレイアウトとファイル命名規則

`$LLM_MEMORY_VAULT/memory/` 配下は `scope` に応じてディレクトリを分ける。
1 論理キー = 1 ファイルの原則により件数がそれほど多くならない前提のため、
フラット配置がふさわしい `scope` はそのままフラットに、グルーピングが
意味を持つ `scope` のみサブディレクトリを切る。

```
$LLM_MEMORY_VAULT/memory/
  _index.md
  <key-slug>.md                     # scope=global（最も典型的なケース）
  projects/
    <project-slug>/
      <key-slug>.md                 # scope=project
  clients/
    <entity-slug>/
      <key-slug>.md                 # scope=client（entity_id でグルーピング。実データ上は稀）
  temporary/
    <key-slug>.md                   # scope=temporary（実データ上は稀、フラットでよい）
```

ファイル名（`.md` を除いた部分は常に `id` と一致する）は
`canonical_memory_id()` が決定的に計算する:

- **典型ケース**（`entity_type="user"` かつ `entity_id="default"`、実質的に
  「既定ユーザーの個人設定」）はスラグに entity 情報を含めない。
  例: `key="preferred_editor"`, `scope="global"` → `preferred-editor.md`
- **非典型ケース**（entity_type が `user` 以外、または entity_id が
  `default` 以外）は `<entity_slug>-<key_slug>` のように entity を前置する。
  この前置ルールはディレクトリ分けとは独立しており、ディレクトリの内外を
  問わず適用される（結果として `scope="project"` かつ非典型 entity の場合、
  ディレクトリ名と前置 entity 名が両方 `lab-web` のように重複して見える
  ケースがあるが、これは意図した挙動である）。
  例: `entity_type="project"`, `entity_id="lab-web"`,
  `key="api_routing_design"`, `scope="project"`, `project_id="lab-web"` →
  `projects/lab-web/project-lab-web-api-routing-design.md`
- **`scope="project"`** はディレクトリ自体が `project_id` による一意性を
  担保するため、旧実装にあった `--<project_slug>` サフィックスは廃止した。
  例: `key="db_migration_status"`, `project_id="myproject"`（典型 entity）→
  `projects/myproject/db-migration-status.md`
- **`scope="client"`** は `entity_id` でグルーピングした
  `clients/<entity_slug>/` 配下に置く。
- **`scope="temporary"`** は `temporary/` 配下にフラットに置く。
- 上記のルールで偶然スラグが衝突した場合（本来起きないはずの edge case）は
  `-2`, `-3` ... の連番を付与してフォールバックする
  （`MarkdownMemoryStore._resolve_free_slug`、ディレクトリを含む相対パス
  全体で衝突判定する）。

### ファイル本体のフォーマット

```markdown
---
type: profile
created: '2026-07-01'
updated: '2026-08-01'
---

# Primary Os

主な OS: Arch Linux

## 変更履歴

- 2026-07-20: Ubuntu → Fedora に変更
- 2026-08-01: Fedora → Arch Linux に変更
```

- YAML frontmatter は `type`/`created`/`updated` の 3 フィールドのみ
  （旧構造・削減理由は「frontmatter の最小化（RDB 構造からの脱却）」章を
  参照）。
- 本文冒頭に `key` を人間可読化した H1 見出し（例: `preferred_editor` →
  `# Preferred Editor`）を追加し、Obsidian のファイル一覧・グラフビューで
  内容が一目でわかるようにしている。見出しは表示専用で、読み込み時に
  自動的に除去され `summary` フィールドには含まれない（往復変換で元の
  `summary` と一致する）。
- 内容が変更されるたびに `## 変更履歴` セクションへ「変更前 → 変更後」の
  1 行が追記される（`format_history_date()`/`render_history_line()`）。
  このセクションは読み込み時に `record["history"]`（文字列のリスト）へ
  パースされ、次回の変更時にはこのリストへ新しい行が追加されてから
  ファイル全体を再書き込みする。

### frontmatter の最小化（RDB 構造からの脱却）

上記の 1 論理キー = 1 ファイル化のあとも、frontmatter 自体は旧 SQLite の
`memories` テーブルの列をほぼ 1:1 で YAML に持ち込んだ構造
（`id`/`memory_type`/`entity_type`/`entity_id`/`scope`/`project_id`/
`confidence`/`salience`/`status`/`valid_from`/`valid_until`/`sources`/
`value` の 13 フィールド）のままだった。Obsidian で人間が読んだときに
ほとんどが意味を持たないか、他の情報と重複していた:

- `id`: ファイル名と重複
- `entity_type`/`entity_id`/`scope`/`project_id`: ディレクトリ配置
  （`memory/projects/<slug>/` 等）が既に表現している
- `confidence`/`salience`: 自動抽出パイプラインのアルゴリズム的スコアで、
  人間の判断材料にならない。「書く価値があるか」は執筆時点のキュレーション
  が担う
- `status: superseded`: 履歴統合（1 論理キー = 1 ファイル化）により概念
  自体が消滅済み
- `valid_from`/`valid_until`: 本文の `## 変更履歴` セクションと重複
- `sources: [{session_id, event_id}]`: ローカル pipeline の内部 ID への
  参照で、人間には無意味
- `value: {value, evidence, source, category}`: 本文の記述と二重管理に
  なっている

これを踏まえ、frontmatter を **`type`/`created`/`updated` の 3 フィールド
のみ**に削減した:

- `type`: `profile`（ユーザーの属性・環境・好み等の静的事実）/
  `feedback`（AI との協働のしかたについての学び・指示。本文は「結論 →
  `**Why:**` → `**How to apply:**`」の構成が有効。既存の
  `~/.claude/skills/*/feedback/` メモリの型を参考にした）/
  `reference`（外部システムへのポインタ）の 3 分類のみ。旧
  `memory_type`（semantic/episodic/procedural）とは別の軸で、
  `semantic → profile`・`procedural → feedback`・`episodic → reference`
  へおおまかに対応するが、`episodic`（直近のコマンド等、時系列的で本来
  「安定記憶」向きではない情報）は3分類のどれにもきれいには収まらず、
  `reference` へ次善の割り当てをしている（`migrate_sqlite_to_markdown.py`
  の `_MEMORY_TYPE_TO_TYPE` 参照）。
- `created`/`updated`: 日付のみ（時刻精度は持たない）。

`entity_type`/`entity_id`/`key`/`value` はもう frontmatter に永続化されず、
`upsert_from_observation()` 呼び出し時の一時的な引数としてのみ使われる
（ファイルパスと本文 H1 見出しの算出にのみ使う）。この結果、
`MarkdownMemoryStore` が読み込み後に返すレコードは
`id`/`type`/`created`/`updated`/`title`/`summary`/`history` に加え、
ディレクトリ配置から機械的に導出した `scope`/`project_id`/`entity_id`
（`entity_id` は `scope="client"` のときのみ意味を持つ）だけを持つ。

この設計変更に伴う副作用:

- **`upsert_from_observation` の同一判定はファイル内容の文字列比較になった。**
  旧来は `value` 辞書の等値比較で「同値か異値か」を判定していたが、
  `value` が無くなったため、代わりに本文の `summary`（要約プロース）の
  文字列一致で判定する。同一なら `updated` のみ更新（confidence 加点に
  相当する処理はもう存在しない）、異なれば旧 `summary` を
  `## 変更履歴` に追記してから本文を新しい `summary` で上書きする。
- **論理キーの同一性は「正規パス + H1 見出し」で確認する。** frontmatter
  にキーが残っていないため、`_find_existing()` は
  `canonical_memory_id()` が導く正規パスを直接読みにいき、そのファイルの
  H1 見出しが `humanize_key(key)` と一致するかだけを確認する（スラグの
  偶発的衝突を検知する簡易チェック。詳細は
  `MarkdownMemoryStore._find_existing` のコメント）。
- **`search`/`get_context` はグローバルスコープの `entity_id` で絞り込め
  なくなった。** 本ストアは単一ユーザーのローカル利用を想定しており
  （後述「安全策」章）、実運用上デフォルトユーザー以外の `entity_id` を
  区別する必要はほぼなかった。`scope="client"` の `entity_id`（ディレクト
  リ名から復元可能）のみ引き続き絞り込める。
- **`history`（履歴検索）コマンドはもう pipeline 層の session/event と
  結合しない。** `sources` が無くなったため、どの発話が根拠だったかを
  遡る機能は失われた。件数が少ない個人利用規模である前提のもと、実害は
  小さいと判断した。

### `forget`（撤回）: archive への物理移動

`forget()` は旧来の `status: deleted` フラグではなく、`memory/archive/`
配下へファイルを**物理的に移動**する（`os.replace`、削除ではない。
ファイル操作ルール上、削除は禁止だが移動は問題ない）。元のディレクトリ
相対構造をそのまま保つ:

```
memory/projects/lab-web/foo.md → memory/archive/projects/lab-web/foo.md
```

- `iter_all()`（および派生する `search`/`get_context`/`_index.md`）は
  `memory/archive/` 配下を常に除外する。
- 一度 archive されたスラグは「空き」として扱われる（`_existing_ids()` が
  archive を除外して判定するため）。同じキーで新しい記憶が書き込まれた
  場合、元の正規スラグをそのまま再利用できる。
- 同じ論理キーが複数回 forget された場合、2 回目以降の archive 先パスは
  1 回目に archive したファイルと衝突する（スラグが再利用可能なため）。
  `os.replace` は移動先の既存ファイルを黙って上書きするため、衝突する
  場合は `_resolve_free_archive_path` がタイムスタンプ付きのファイル名
  （例: `foo-20260802-093000.md`）へ退避してから移動する。これにより
  過去の archive ファイルが上書きで消えることはない。
- 物理削除する `delete()`（`cleanup` サブコマンドが使う、
  `recent_summary` の恒久除去専用）は今回のスコープ外で変更していない。

### `_index.md`（生成専用の索引）

`$LLM_MEMORY_VAULT/memory/_index.md` は `memory/` 配下を再帰的に走査して
全メモリ（`archive/` 配下を除く）を一覧化した索引ファイルで、`write`/
`upsert_from_observation`/`forget`/`delete` などの書き込み系操作の最後に
毎回再生成される。ディレクトリ構成を反映して `## Global` /
`## Project: <project_id>` / `## Client: <entity_id>` / `## Temporary` で
グルーピングし（該当データがないセクションは省略）、各行は
`- [タイトル](相対パス.md) — summary 抜粋` の形式にする。リンクはネストした
ディレクトリでもそのまま `id`（ディレクトリを含む相対パス）を使えば
`_index.md` の設置場所（`memory/` 直下）から正しく解決される。このファイルは
ストア自身が読み取りに使うことはなく、あくまで人間が Vault をブラウズする
ための派生物として扱う（`iter_all()` は `_index.md` 自体を memory レコード
として解釈しない）。

### 決定的スラグ生成による Syncthing コンフリクト低減

旧実装ではファイル名を「論理キー（entity_type + entity_id + key + scope +
project_id）の SHA256 ハッシュ」から導出しており、これは人間には読めない
代わりに次の性質を持っていた: 同じ論理キーに対して複数端末が同時にオフライン
で書き込んでも、同じファイル名に収束する（Syncthing が別々のファイルとして
複製せず、単一ファイルの sync-conflict として検出できる）。

現在の実装ではこの性質を、ハッシュではなく**キー自体から決定的に導出した
スラグ + ディレクトリパス**に置き換えることで維持している。典型ケース・
非典型ケース・scope によるディレクトリ分けいずれも、同じ論理キーからは
常に同じ候補パスが計算される（`canonical_memory_id()`）。1 論理キー = 1
ファイルの原則と組み合わさることで、「値が変わっても同じファイルを
上書きし続ける」という決定性がむしろ強化されている（旧実装は値が変わる
たびに新しい archival ファイルが増えるため、2 台の端末が同時にオフラインで
別々の値へ更新すると異なる archival ファイル名が生成され収束しなかった。
現在は履歴が同一ファイル内に追記されるだけなので、そのファイル 1 つの
sync-conflict として検出できる）。

例外は、たまたま既存の別ファイルとスラグが衝突していた場合の `-2`/`-3`
フォールバックで、これはローカルのディレクトリ状態に依存するため、理論上は
2 台の端末で同じ論理キーが異なるスラグに解決される可能性がゼロではない。
ただしこれは「本来ほぼ起こらない edge case」であり、発生しても Vault の
実データが数十〜数百件規模である現状では実害は小さいと判断し許容している。

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

#### なぜ今回は方針を覆したか

上記の却下理由（検索・整合性・同時更新・削除履歴に弱い）は今も一般論として正しい。
実際に `memories` 層を Vault Markdown へ移行したのは、この却下理由が効かない
条件が揃ったため:

- **書き込み経路を CLI に一本化し、自由な直接編集を前提にしなかった**
  却下時に想定していた「JSON/Markdown 直置き」は人間やスクリプトが自由に
  ファイルを編集する運用を想定していたため、整合性・削除履歴の管理が困難
  だった。実際の移行では `MarkdownMemoryStore` が唯一の書き込み口のままで、
  YAML frontmatter に `status` / `valid_from` / `valid_until` などの整合性・
  削除履歴フィールドを引き続き持たせている。ファイルは「人間が読める」が
  「人間が自由に書き換える」ことは前提にしていない。
- **対象を `memories` 層のみに限定した**
  高頻度に追記される `events` / `observations` / `sessions` 層は従来どおり
  ローカルの JSON/JSONL ファイル（`LocalPipelineStore`、非同期・非 Vault）に
  留め、Syncthing 同期対象にしていない。同期・可読性が必要な「安定記憶」と、
  高頻度で使い捨てに近い「生ログ」を最初から分離しているため、Markdown 化の
  対象範囲が小さく、検索性能・書き込み頻度の懸念が生じにくい。
- **複数端末同時実行という、そもそも実現していなかった前提が外れた**
  却下時に懸念していた「同時更新」は、複数端末から同時に書き込みが発生する
  運用を想定していた。実際には cross-machine 同期は Syncthing 経由の非同期
  反映であり、同一時刻の同時書き込みは元々ほぼ発生しない運用だった。この
  前提のズレに気づいたため、決定的スラグ生成（後述）で「同時書き込みが
  発生しても収束する」設計にすれば十分と判断した。
- **検索は件数が少ない前提で妥協できた**
  `memories` 層は個人利用規模（数十〜数百件）を想定しており、SQLite FTS5 の
  ような全文検索エンジンがなくても `iter_all()` の線形スキャンで実用上
  問題ない。件数が数万件規模に増えたら再度 DB 化を検討する。

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
