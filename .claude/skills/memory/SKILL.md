---
name: memory
description: 共有メモリ基盤の設定・構成・障害診断・復旧を扱う。保存されない、MCP が起動しない、検索結果や同期に問題がある、memory.py や保存先を変更したい場合に使う。日常の読み書きは shared-memory、セッション抽出は memory-extract を使う。
---

# Memory

共有メモリ基盤の運用スキル。日常の読み書きやセッションからの抽出を自分で実行するスキルではなく、設定・実装・障害を調べるときに使う。

## 責務

- `memory/README.md` と `memory/DETAILS.md` を正本として、MCP・CLI・保存先の構成を確認する
- `LLM_MEMORY_CONFIG`、`LLM_MEMORY_VAULT`、`LLM_MEMORY_LOCAL_DIR`、`LLM_MEMORY_QUEUE_DIR` の解決結果を確認する
- MCP の起動失敗、接続失敗、権限・承認エラー、保存先の I/O エラーを原因別に切り分ける
- CLI と MCP が同じ明示設定・同じ Vault を使っていることを確認する
- 試験・検証で CLI を実行するときは保存先の隔離を確認する（「試験時の保存先隔離」参照）
- ロック競合、Syncthing 競合、部分失敗、索引不整合を調査・復旧する
- 設定変更後に MCP サーバーとクライアントを再起動し、接続と検索を検証する

## 境界

- 通常の `get_context`、`search`、`history`、`write_memory`、`forget` は `shared-memory` を使う
- セッションの `list_unextracted` → 抽出 → `mark_extracted` は `memory-extract` を使う
- MCP が使えないときに CLI へ迂回する場合も、同じ明示設定を確認する。設定エラー・権限エラーを別 Vault で隠さない

## 試験時の保存先隔離

実データを触りうる CLI や Python 直呼びを試験目的で実行するときは、次を守る。

- `LLM_MEMORY_VAULT`、`LLM_MEMORY_LOCAL_DIR`、`LLM_MEMORY_QUEUE_DIR`、`LLM_MEMORY_CONFIG` の 4 変数をすべて `env VAR=...` で一時ディレクトリへ明示上書きしてから実行する。環境変数は `config.toml` より優先されるため、設定ファイルの差し替えだけでは隔離できない
- 実行前に `env | grep LLM_MEMORY` で環境側の値を確認し、書き込み前に `search` で一時 Vault が空であることを確かめる
- 検証後に実 Vault のファイル一覧と `_index.md` のチェックサムが不変であることを確認する
- 隔離の参照実装は `scripts/memory-mcp-check.sh`

## 診断の順序

1. `codex mcp get shared-memory` または `claude mcp list` で登録と接続状態を確認する
2. MCP 登録の command、args、環境変数、実行ファイルの絶対パスを確認する
3. Vault が明示され、設定ファイルの構文・型・権限に問題がないことを確認する
4. Codex は `approval_policy` と MCP の `default_tools_approval_mode`、Claude Code は MCP 登録と `permissions.defaultMode` を確認する
5. `get_context`、`search`、`history` で読み出しを確認し、保存失敗時は `search` / `history` で部分保存の有無を確認する
6. 本文が保存され索引だけ失敗した場合は、本文を正本として索引を再生成する

同一端末のプロセス間ロックは端末内の競合だけを扱う。Syncthing や Obsidian による別端末・手動編集の競合は別途確認し、競合ファイルを無断で削除しない。

詳細な保存形式、競合、復旧手順、CLI は [`memory/DETAILS.md`](../../../memory/DETAILS.md) と [`memory/README.md`](../../../memory/README.md) を参照する。
