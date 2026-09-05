# shared-memory 詳細仕様

この文書は [`memory/README.md`](README.md) の補足です。利用者向けの機能説明、セットアップ、接続確認は README を参照してください。ここでは保存形式、内部処理、競合時の扱い、実装ファイル、テストなどの詳細をまとめます。

## アーキテクチャ

### Vault層（memories・安定記憶）

- 保存先: 明示設定の Vault 配下の `memory/`（設定方法は下記。設定なしの従来 CLI のみ `~/.agents/memory/vault/` へフォールバック）
- Syncthing 同期対象。Obsidian 等のノートアプリで人間が直接閲覧・編集できることを意図している
- 1論理キー（`entity_type` + `entity_id` + `key` + `scope` + `project_id`）= 1 Markdown ファイル
- frontmatter は `type`（`profile` / `feedback` / `reference` の3種）・`created`・`updated` のみの最小構成
- 本文は人間可読な説明文＋任意で「## 変更履歴」セクション（値が変わった場合のみ、変更前の値と日付を追記する。新しいファイルは作らない）
- ディレクトリ構成（`scope` ごとにグルーピング）:
  - `memory/` 直下には生成物の `memory/_index.md` **だけ**を置く。具体的な記憶ファイルは必ず分類ディレクトリ内に置く
  - `memory/global/<key>.md` — `scope=global`
  - `memory/projects/<project-slug>/<key>.md` — `scope=project`
  - `memory/clients/<entity-slug>/<key>.md` — `scope=client`
  - `memory/temporary/<key>.md` — `scope=temporary`
- `forget`（記憶の撤回）は物理削除ではなく、元のディレクトリ相対構造を保ったまま `memory/archive/` へファイルを移動する
- `forget`/`write_memory` などが受け取る記憶 ID（CLI の `--memory-id`、MCP の `memory_id`）は `global/<slug>`・`projects/<project>/<slug>`・`clients/<entity>/<slug>`・`temporary/<slug>` のようにディレクトリ階層を含む相対パス形式のみを受け付ける。ディレクトリを省いた裸のスラグ（例: `<slug>` 単体）は保存領域内の scope を一意に特定できないため拒否される（`store_paths.validate_memory_id`）。ID は `search`/`history`/`get_context` の結果に含まれる `id` フィールドをそのまま使う
- `memory/_index.md` は全 active 記憶の索引として自動生成・維持される

### local層（sessions/events/observations・pipeline）

- 保存先: `$LLM_MEMORY_LOCAL_DIR`（既定 `~/.agents/memory/local/`）
- Syncthing 同期対象外（Vault とは別ディレクトリで、生ログ・中間データのため同期不要）
- 構成: `sessions/<id>.json`（セッションメタ）、`events/<id>.jsonl`（生ログ、append-only）、`observations/<id>.jsonl`（抽出候補）、`logs/`（retrieval / deletions の監査ログ）、キューは `queue_dir` の独立した保存先（既定 `~/.cache/llm-memory/queue`）

## 競合と部分失敗

同一端末の CLI/MCP は共通ストアでプロセス間ロックを取得し、更新・履歴追記・索引生成を直列化します。不正 ID、保存領域外のパス、操作対象内のリンクは拒否します。ただし実行中の悪意あるファイルシステム差し替えを防ぐ OS サンドボックスではありません。

別端末の Syncthing 同時編集と Obsidian の直接編集はこのロックに参加しません。同じ記憶を複数端末から同時に更新しない運用を基本とし、競合発生時は書き込みを止め、本文と変更履歴を比較して統合します。競合ファイルの削除は利用者の判断後に行います。

Vault と local の複数ファイル更新は一括ロールバックされません。エラー時も memory や event の一部が保存済みの場合があります。再送する前に `search` / `history` と元セッションの記録を確認し、未処理部分だけを再開してください。抽出元セッションを処理済みにして失敗を隠さないでください。索引は派生物であり、本文を正本として復旧します。ほかの書き込みと同期競合を解消した後、同じ明示設定でリポジトリルートから再生成できます。

```bash
uv run --locked --project memory python -c 'import sys; sys.path.insert(0, "memory"); from markdown_store import MarkdownMemoryStore; from memory_config import resolve_vault_dir; MarkdownMemoryStore(resolve_vault_dir()[0]).rebuild_index()'
```

ロックファイルは同期外の Unix `$XDG_CACHE_HOME/llm-memory/locks`（既定 `~/.cache/llm-memory/locks`）、Windows `%LOCALAPPDATA%/llm-memory/locks` に置きます。ファイルの存在ではなく OS のロックで所有を判定するため、稼働中にロックディレクトリを削除しないでください。

既知の制約: `MarkdownMemoryStore._assert_writable()` は書き込みのたびに Vault 配下を `rglob("*")` で全走査してリンク・保存領域外パスを検査するため、Vault が大きくなるほど1回の書き込みコストが線形に増える。現時点では対応せず、将来 Vault の記憶数が実運用上問題になる規模に達した場合の最適化候補として記録するに留める。

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
| `mcp_server.py` | FastMCP の stdio サーバー。7 ツールを共通ロジックへ接続 |
| `check_mcp.py` | MCP SDK の stdio クライアントで `mcp_server.py` を実子プロセスとして起動し、initialize〜tools/call を実往復させるハンドシェイク試験。`test_mcp_server.py` の live round trip テストと `scripts/memory-mcp-check.sh`（延いては導入スクリプト・`make memory-mcp-check`）の両方から呼ばれる。実際に `write_memory` 等を実行するため、呼び出し側が Vault/local/queue/config を隔離する責任を負う |
| `memory_config.py` | CLI/MCP 共通の厳格な設定・保存先解決 |
| `store_lock.py` | CLI・両ストア・移行処理が共有するプロセス間ファイルロック（`$XDG_CACHE_HOME`/`%LOCALAPPDATA%` 配下の同期外ディレクトリにロックファイルを置く） |
| `store_paths.py` | session ID・記憶 ID・保存領域内パスの検証（traversal・絶対パス・リンクの拒否） |
| `memory.py` | CLI本体。サブコマンド: `init-db` / `start-session` / `append-event` / `end-session` / `extract` / `consolidate` / `search` / `history` / `get-context` / `forget` / `queue-session` / `flush-queue` / `cleanup` / `list-unextracted` / `write-memory` / `mark-extracted` |
| `markdown_store.py` | `MarkdownMemoryStore`。Vault層の読み書き・upsert・forget・索引生成を担当 |
| `local_store.py` | `LocalPipelineStore`。local層（sessions/events/observations/logs）の読み書きを担当 |
| `migrate_sqlite_to_markdown.py` | 旧SQLiteからの一括移行スクリプト。dry-run が既定で `--apply` で実際に書き込む。冪等 |
| `test_*.py` | unittest ベースのテストスイート |
| `run-python.sh` | PyYAML が import できる Python で引数をそのまま実行するラッパー。`memory.py` の唯一のサードパーティ依存が PyYAML であり、システムの `python3` に入っていない環境でも動くように、`LLM_MEMORY_PYTHON` → システム `python3` → `uv` → `mise x uv` の順で解決する |
| `codex-memory-run.sh` / `codex-memory-start.sh` / `codex-memory-stop.sh` / `codex-memory-log.sh` | `codex` コマンドをラップし、セッション開始時に `start-session`、終了時（trap EXIT）に `end-session --extract --consolidate` を自動実行する |
| `hook-stop-memory.sh` | Claude Code の Stop hook 用スクリプト。現状 `.claude/settings.json` に未配線で自動実行されない。Claude Code 側は `/shared-memory` スキル経由の手動判断による読み書きが基本 |
| `llm-shared-memory-design.md` | 設計ドキュメント。SQLite採用の経緯、後にファイルベースへ移行した理由の記録を含む |
| `../scripts/memory-mcp-check.sh` | `check_mcp.py` を一時 Vault/local/queue/config に隔離して呼ぶ共通ヘルパー。`make memory-mcp-check` と `deploy-memory-mcp.sh` の両方から使われ、実 Vault/実 local を汚染しない |
| `../scripts/deploy-memory-mcp.sh` / `../scripts/deploy-memory-mcp.ps1` | 各 OS の MCP クライアント登録エントリを生成する入口。uv 同期 → `memory-mcp-check.sh`（隔離済みハンドシェイク）→ `memory_mcp_config.py` の順に呼ぶ |
| `../scripts/memory_mcp_config.py` | クライアント設定（Claude Code/Desktop の JSON、Codex の TOML）への `shared-memory` エントリの生成・保全付き適用 |

## 依存関係

MCP と全テストはロック済み uv 環境で実行します。MCP SDK と PyYAML を依存管理し、登録設定の TOML 編集には tomlkit を使います。従来の `run-python.sh` は `LLM_MEMORY_PYTHON` → PyYAML 利用可能なシステム Python → uv（ロック済み `memory/` project）→ mise 経由の同 uv、の順で解決する互換用です。uv/mise フォールバックは MCP・全テストと同じ `memory/pyproject.toml` + `uv.lock` の環境を使うため、CLI が使う PyYAML のバージョンも他の経路と揃います（以前の `--no-project --with pyyaml` は呼ぶたび使い捨て環境を用意していました）。古い Python が TOML を扱えない場合、設定があるのに無視して実行せず、対応 Python/uv へ切り替えます。

## 使用例

```bash
memory/run-python.sh memory/memory.py search --query "エディタ"
memory/run-python.sh memory/memory.py get-context --user-id default --project-id my-project
memory/run-python.sh memory/memory.py write-memory --session-id <id> --memory-type profile --key preferred_editor --summary "好みのエディタ: Neovim" --scope global
memory/run-python.sh memory/memory.py forget --memory-id <id> --reason "古くなったため"
memory/run-python.sh memory/memory.py migrate-layout
```

`migrate-layout` は旧形式の `memory/<key>.md` を `memory/global/<key>.md` へ安全に移す一回限りの移行コマンドです。移動先に同名ファイルがある場合は上書きせずエラーにします。

## テスト

```bash
uv run --locked --project memory python -m unittest discover -s memory -p "test_*.py"
```

## 関連スキル

- `memory` スキル（`.claude/skills/memory/SKILL.md`）: 基盤のトラブルシューティング・DB操作全般
- `shared-memory` スキル（`.claude/skills/shared-memory/SKILL.md`）: 日常的な読み書きインターフェース
- `memory-extract` スキル（`.claude/skills/memory-extract/SKILL.md`）: LLMベースのセッション抽出バッチ処理
