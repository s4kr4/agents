# memory

Codex や Claude Code など複数の LLM クライアントからセッション横断で記憶を共有するための基盤です。かつては SQLite ベースで実装していましたが、現在はファイルベースの2層構成（Vault層 / local層）に移行済みです。

## アーキテクチャ

### Vault層（memories・安定記憶）

- 保存先: `$LLM_MEMORY_VAULT/memory/`（環境変数未設定時は `~/.agents/memory/vault/` にフォールバックし、stderr に警告を出す）
- Syncthing 同期対象。Obsidian 等のノートアプリで人間が直接閲覧・編集できることを意図している
- 1論理キー（`entity_type` + `entity_id` + `key` + `scope` + `project_id`）= 1 Markdown ファイル
- frontmatter は `type`（`profile` / `feedback` / `reference` の3種）・`created`・`updated` のみの最小構成
- 本文は人間可読な説明文＋任意で「## 変更履歴」セクション（値が変わった場合のみ、変更前の値と日付を追記する。新しいファイルは作らない）
- ディレクトリ構成（`scope` ごとにグルーピング）:
  - `memory/<key>.md` — `scope=global`
  - `memory/projects/<project-slug>/<key>.md` — `scope=project`
  - `memory/clients/<entity-slug>/<key>.md` — `scope=client`
  - `memory/temporary/<key>.md` — `scope=temporary`
- `forget`（記憶の撤回）は物理削除ではなく、元のディレクトリ相対構造を保ったまま `memory/archive/` へファイルを移動する
- `memory/_index.md` は全 active 記憶の索引として自動生成・維持される

### local層（sessions/events/observations・pipeline）

- 保存先: `$LLM_MEMORY_LOCAL_DIR`（既定 `~/.agents/memory/local/`）
- Syncthing 同期対象外（Vault とは別ディレクトリで、生ログ・中間データのため同期不要）
- 構成: `sessions/<id>.json`（セッションメタ）、`events/<id>.jsonl`（生ログ、append-only）、`observations/<id>.jsonl`（抽出候補）、`logs/`（retrieval / deletions の監査ログ）、`queue/`（書き込み失敗時のフォールバックキュー）

## 環境変数

| 変数 | 用途 | 既定値 |
| --- | --- | --- |
| `LLM_MEMORY_VAULT` | Vault層の保存先（Syncthing 同期下のディレクトリを指定する想定） | 未設定時は `~/.agents/memory/vault/` にフォールバック |
| `LLM_MEMORY_LOCAL_DIR` | local層の保存先 | `~/.agents/memory/local/` |
| `LLM_MEMORY_QUEUE_DIR` | 書き込み失敗時のフォールバックキュー | `~/.cache/llm-memory/queue` |

## 記憶が書き込まれる2つの経路

### (a) ルールベース自動抽出

`memory.py` 内の `build_candidates()` が、`LANGUAGE_PREFERENCES` / `EDITOR_PREFERENCES` / `OS_PREFERENCES` 等の固定辞書（例: `{"vim": "Vim", "neovim": "Neovim", ...}`）で会話テキストとの単純なキーワード一致を見る、粗いルールベース抽出です。この辞書は完全に静的なハードコードであり、共有メモリへのデータ保存によって動的に更新されることはありません。新しいキーワードを認識させるには `memory.py` のコード自体を変更する必要があります。

Codex 側は `codex-memory-run.sh` 経由でセッション終了時に自動実行されます。Claude Code 側は Stop hook（`hook-stop-memory.sh`）が用意されているものの `.claude/settings.json` に未配線のため、自動実行されません。

### (b) LLM判断による書き込み

- `/shared-memory` スキル: 明示的なトリガー語句、またはモデルが「長期的に有効な事実」と判断した際にプロアクティブに書き込む
- `/memory-extract` スキル: 未処理セッション（`list-unextracted`）を LLM が読み、長期的に有効な知識だけを判断して `write-memory` する、バッチ処理的な運用

どちらも辞書に縛られず柔軟ですが、自動実行はされず明示的な呼び出しが必要です。

## 主なファイル

| ファイル | 役割 |
| --- | --- |
| `memory.py` | CLI本体。サブコマンド: `init-db` / `start-session` / `append-event` / `end-session` / `extract` / `consolidate` / `search` / `history` / `get-context` / `forget` / `queue-session` / `flush-queue` / `cleanup` / `list-unextracted` / `write-memory` / `mark-extracted` |
| `markdown_store.py` | `MarkdownMemoryStore`。Vault層の読み書き・upsert・forget・索引生成を担当 |
| `local_store.py` | `LocalPipelineStore`。local層（sessions/events/observations/logs）の読み書きを担当 |
| `migrate_sqlite_to_markdown.py` | 旧SQLiteからの一括移行スクリプト。dry-run が既定で `--apply` で実際に書き込む。冪等 |
| `test_*.py` | unittest ベースのテストスイート |
| `codex-memory-run.sh` / `codex-memory-start.sh` / `codex-memory-stop.sh` / `codex-memory-log.sh` | `codex` コマンドをラップし、セッション開始時に `start-session`、終了時（trap EXIT）に `end-session --extract --consolidate` を自動実行する |
| `hook-stop-memory.sh` | Claude Code の Stop hook 用スクリプト。現状 `.claude/settings.json` に未配線で自動実行されない。Claude Code 側は `/shared-memory` スキル経由の手動判断による読み書きが基本 |
| `llm-shared-memory-design.md` | 設計ドキュメント。SQLite採用の経緯、後にファイルベースへ移行した理由の記録を含む |

## 使用例

```bash
python3 memory/memory.py search --query "エディタ"
python3 memory/memory.py get-context --user-id default --project-id my-project
python3 memory/memory.py write-memory --session-id <id> --memory-type profile --key preferred_editor --summary "好みのエディタ: Neovim" --scope global
python3 memory/memory.py forget --memory-id <id> --reason "古くなったため"
```

## テスト

```bash
python3 -m unittest discover -s memory -p "test_*.py"
```

## 関連スキル

- `memory` スキル（`.claude/skills/memory/SKILL.md`）: 基盤のトラブルシューティング・DB操作全般
- `shared-memory` スキル（`.claude/skills/shared-memory/SKILL.md`）: 日常的な読み書きインターフェース
- `memory-extract` スキル（`.claude/skills/memory-extract/SKILL.md`）: LLMベースのセッション抽出バッチ処理
