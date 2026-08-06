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

Codex にはセッション開始・終了時に自動でメモリ操作を行うシェルラッパーを用意しています。Claude Code 側は毎ターン自動保存する仕組みはなく、`shared-memory` スキル経由でモデルが必要と判断したときに手動で読み書きします。DB に書き込めない環境向けのファイルキューへのフォールバック（`queue-session` / `flush-queue`）は pipeline 層の仕組みとして維持されています。

詳しい使い方は `memory` スキル（`.claude/skills/memory/SKILL.md`）および `shared-memory` スキル（`.claude/skills/shared-memory/SKILL.md`）を参照してください。

```bash
make memory-init  # Vault/local ディレクトリの初期化
make memory-demo  # 最小デモ
```
