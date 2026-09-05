# memory

Codex、Claude Code、Claude Desktop など複数の LLM クライアントから、セッションをまたいで記憶を共有するローカル基盤です。保存先は Markdown の Vault と端末ローカルの pipeline データに分かれています。

## できること

- クライアント間で安定した記憶を共有する
- `get_context`、`search`、`history` で既存の記憶を参照する
- `write_memory` でユーザーの好み、プロジェクトの決定事項、協働上の知見を保存する
- `forget` で記憶を撤回する
- `list_unextracted` と `mark_extracted` でセッションからの知識抽出を管理する
- Syncthing 対象の Vault と、同期しない local / queue を分離する
- Codex と Claude Code から同じ stdio MCP サーバーを利用する

日常の読み書きは `shared-memory` MCP を使います。CLI は初期化、移行、キュー処理、診断に利用します。会話を無差別に保存する仕組みではなく、`shared-memory` / `memory-extract` スキルが長期的に有用と判断した内容を保存します。

## MCP の導入

日常の読み書きは各端末で起動する `shared-memory` stdio MCP サーバーを使います。7 ツールは `get_context`、`search`、`history`、`write_memory`、`forget`、`list_unextracted`、`mark_extracted` です。CLI は初期化・移行・キュー・診断用にも維持します。

これは各 OS・アプリからのアクセス経路を共通化する構成です。権限を一か所に集約するリモートサービスではありません。接続登録と各アプリの承認設定は必要です。Claude のクラウドコネクターへこの stdio サーバーを URL 登録することはできません。

### 1. uv と依存環境

[uv 公式インストール手順](https://docs.astral.sh/uv/getting-started/installation/)で OS に対応した uv を導入し、`uv --version` で確認します。Windows でも Bash・make は不要です。リポジトリルートで次を実行します。

```text
uv sync --locked --project memory
```

MCP は `memory/pyproject.toml` と `uv.lock` の環境を使用します。旧 CLI ラッパーが選ぶシステム Python と同じ環境とは限りません。

### 2. 端末ごとの保存先設定

設定ファイルは Syncthing で同期しません。既定の探索先は以下です。

| OS | 設定ファイル |
| --- | --- |
| Ubuntu・macOS | `$XDG_CONFIG_HOME/llm-memory/config.toml`、未設定時 `~/.config/llm-memory/config.toml` |
| native Windows | `%APPDATA%/llm-memory/config.toml`、未設定時 `~/AppData/Roaming/llm-memory/config.toml` |
| 任意の OS | `LLM_MEMORY_CONFIG` を指定した場合はそのファイル |

Unix の例:

```toml
vault = "~/Syncthing/llm-vault"
local_dir = "~/.agents/memory/local"
queue_dir = "~/.cache/llm-memory/queue"
```

native Windows の例（ユーザー名と保存先を実環境に置換）:

```toml
vault = 'C:/Users/yourname/Syncthing/llm-vault'
local_dir = 'C:/Users/yourname/.agents/memory/local'
queue_dir = 'C:/Users/yourname/AppData/Local/llm-memory/queue'
```

`vault` は `memory/` を入れる親ディレクトリです。上の Unix 例では記憶が `~/Syncthing/llm-vault/memory/` に保存されます。

優先順位は明示引数 > 環境変数 > TOML > 既定値。環境変数は `LLM_MEMORY_VAULT`、`LLM_MEMORY_LOCAL_DIR`、`LLM_MEMORY_QUEUE_DIR` です。`~` と環境変数を展開した後の絶対パスを要求し、相対パス・未展開変数・空文字・未知キー・型不正を拒否します。既定設定が存在しない場合だけ設定なしとして扱います。明示設定の不存在、既存設定の構文・読み取りエラーは停止理由です。環境変数があっても壊れた設定を無視しません。

MCP は Vault の明示設定を必須とします。従来 CLI の設定なしフォールバックは互換用に残っていますが、MCP の代替として CLI を使う際は同じ明示設定を渡してください。設定変更後は MCP サーバーを再起動します。

WSL と native Windows は別ホストです。Python・uv・パス・設定ファイルを分け、Windows アプリへ WSL 用の `/home/...` コマンドをそのまま登録しません。GUI はターミナルの環境変数を継承しない場合があるため、既定探索先の設定ファイルを利用するか、アプリにも同じ `LLM_MEMORY_CONFIG` を設定します。

### 3. クライアント登録内容を生成

Ubuntu・macOS・WSL（リポジトリルートから）:

```bash
scripts/deploy-memory-mcp.sh --client claude-code
scripts/deploy-memory-mcp.sh --client codex
scripts/deploy-memory-mcp.sh --client claude-desktop
```

native Windows の PowerShell:

```powershell
& ./scripts/deploy-memory-mcp.ps1 --client claude-desktop
& ./scripts/deploy-memory-mcp.ps1 --client codex
```

入口は uv とリポジトリの絶対パスを解決し、依存同期と起動確認後に登録内容を生成します。既定ではユーザー設定を書き換えません。生成された command/args を対象アプリへ登録します。Claude Code/Codex CLI が未導入でも Desktop 用の設定を生成できます。

生成器が出力するのは `command` / `args` だけです。保存先の環境変数はアプリへ明示的に渡してください。アプリが端末の `LLM_MEMORY_*` 環境変数または既定の TOML 設定を継承する保証はありません。初回登録では、次の3変数を MCP サーバーの環境変数として登録する方法を推奨します。

Claude Code の user スコープ登録（Ubuntu・WSL・macOS）:

```bash
claude mcp add -s user shared-memory \
  -e LLM_MEMORY_VAULT=/absolute/path/to/vault \
  -e LLM_MEMORY_LOCAL_DIR=/absolute/path/to/local \
  -e LLM_MEMORY_QUEUE_DIR=/absolute/path/to/queue \
  -- /absolute/path/to/uv run --locked \
  --project /absolute/path/to/.agents/memory \
  /absolute/path/to/.agents/memory/mcp_server.py
```

Codex は `~/.codex/config.toml` の既存の `[mcp_servers.shared-memory]` に、生成された `command` / `args` と環境変数を設定します。MCP ツールを自動実行する設定は次のとおりです。

```toml
approval_policy = "on-request"

[mcp_servers.shared-memory]
default_tools_approval_mode = "auto"
```

同じセクションの `command` / `args` と `[mcp_servers.shared-memory.env]` に、端末の絶対パスを設定してください。`approval_policy = "never"` のままだと、承認対象になった MCP ツールを実行できません。

非機密の設定ファイルへ適用する場合は `--apply --config <対象ファイル>` を明示します。既存ファイルには `--non-secret-config` の申告も必要です。資格情報を含む設定をこのスクリプトで読み書きせず、生成結果をアプリ自身で登録してください。リンク先は暗黙に置換せず、必要なら実体ファイルを明示します。

更新対象は `shared-memory` の command/args だけです。環境変数・タイムアウト・権限・他サーバーの設定を保持し、変更不要なら書き込みません。外部変更を検出すると停止し、適用後の検証失敗時は外部変更がない場合だけ復旧します。権限・承認設定は自動変更しません。`forget` は記憶を撤回するため、利用者が確認できる承認設定を推奨します。

既存設定への適用では、生成器は環境変数・承認設定を変更しません。したがって、初回登録時は上記のクライアント固有コマンドまたはアプリの設定画面で環境変数と承認設定を登録し、生成器は command/args の更新に使います。

| OS・ホスト | 対象ツール | 登録・確認 | 実機確認の扱い |
| --- | --- | --- | --- |
| Ubuntu / WSL | Claude Code、Codex CLI | 各 CLI に生成した stdio command/args を登録、ツール一覧を確認 | 自動試験と実クライアント確認は別。実機結果は実装計画に記録 |
| Ubuntu | Codex / Claude Desktop | 対象版のローカル MCP 設定で登録 | GUI は未検証、対象版の対応確認が必要 |
| macOS | Claude Code、Codex CLI / Desktop、Claude Desktop | 生成設定を各ホストへ登録 | macOS 実機は未検証 |
| native Windows | 対応 CLI、Codex / Claude Desktop | PowerShell で生成、Windows 側ホストへ登録 | Windows 実機は未検証 |

Codex は同一ホストの CLI・IDE・Desktop で MCP 設定を共有します。[Codex MCP 資料](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)を参照してください。Claude Desktop の具体的な設定場所は対象版の[ローカル MCP 手順](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)で確認します。

### 4. 再起動と読み書き確認

設定変更後は `resume` ではなくクライアントを完全終了して新しく起動します。Codex は `codex mcp get shared-memory`、Claude Code は `claude mcp list` で `shared-memory` が接続済み（`✔ Connected`）になり、7 ツールが列挙されることを確認します。`get_context` で既存の記憶が見えるか確認してください。初回の書き込み試験は一時 Vault/local/queue の設定で行い、`write_memory` → `search` → 不正 ID のエラー → 再検索の順に、エラー後も使えることを確認します。テスト環境の成功と本番設定の成功を混同しないでください。

日常の `write_memory` は `session_id` 省略可能です。セッション抽出では元セッションの ID を必ず渡し、プロジェクトを保持します。すべての保存に成功した後だけ `mark_extracted` を呼びます。

## 詳細仕様

保存形式、競合と部分失敗、抽出経路、CLI の詳細、実装ファイル、依存関係、テストは [`DETAILS.md`](DETAILS.md) に分離しています。

## 関連スキル

- `memory` スキル（`.claude/skills/memory/SKILL.md`）: 基盤のトラブルシューティング・移行・診断
- `shared-memory` スキル（`.claude/skills/shared-memory/SKILL.md`）: 日常的な読み書き
- `memory-extract` スキル（`.claude/skills/memory-extract/SKILL.md`）: セッションからの知識抽出
