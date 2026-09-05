# agents

Claude Code のグローバル設定を管理するリポジトリです。

## 概要

`~/.claude/` に配置する Claude Code の設定ファイル（CLAUDE.md、エージェント、スキル、ルール）を dotfiles として一元管理します。

## ディレクトリ構成

```
.
├── .claude/
│   ├── CLAUDE.md          # グローバル開発ガイドライン
│   ├── settings.json      # Claude Code 設定
│   ├── agents/            # カスタムサブエージェント定義
│   ├── skills/            # カスタムスキル定義
│   └── rules/             # 開発ルール・ガイドライン
├── memory/                # 共有メモリ関連のCLI、hook、設計資料
├── scripts/
│   └── deploy.sh          # デプロイスクリプト
└── Makefile
```

## セットアップ

```bash
git clone <repo-url> ~/.agents
cd ~/.agents
make deploy
```

`make deploy` を実行すると、`.claude/` 配下のファイルが `~/.claude/` にシンボリックリンクとして展開されます。

## コマンド

```bash
make deploy  # Claude Code 設定をデプロイ（シンボリックリンク作成）
make update  # 最新を pull してデプロイ
```

## 共有メモリ

ファイルベースの 2 層構成で Codex や Claude Code など複数の LLM クライアントからセッション横断の記憶を共有する仕組みです。

- Vault（`memories`。Syncthing 同期対象）: 安定した記憶を Obsidian Vault 配下に Markdown で保存。1 論理キー = 1 ファイルで、値の変遷は同一ファイル内の変更履歴に追記する
- local（`sessions`/`events`/`observations`。同期対象外）: 生ログ・pipeline 層のデータを `~/.agents/memory/local/` 配下にファイルとして保存

日常の読み書きは `shared-memory` stdio MCP サーバー経由で行います。各端末に uv と保存先の TOML を設定し、各 CLI・Desktop アプリへ登録します。OS ごとの保存先をアプリから切り離せますが、端末ごとの導入とアプリの承認設定は残ります。リモート常駐サービスへの権限集約ではありません。

[導入・OS 別の設定・検証状況](memory/README.md#mcp-の導入)に Ubuntu/macOS/Windows の手順をまとめています。native Windows では PowerShell の入口を使用でき、Bash・make は不要です。GUI と Windows/macOS の実機確認は自動試験とは区別して扱います。

保存形式や競合時の扱いなどの内部仕様は [`memory/DETAILS.md`](memory/DETAILS.md) にまとめています。

Codex のセッション用シェルラッパー、初期化・移行・キュー処理の CLI は維持しています。CLI を MCP の代わりに使うときも同じ明示設定が必要です。壊れた設定や権限エラーを別 Vault への保存で回避しません。日常操作は `shared-memory`、履歴の知識抽出は `memory-extract`、診断は `memory` スキルを参照してください。

```bash
make memory-init  # Vault/local ディレクトリの初期化
make memory-demo  # 最小デモ
```
