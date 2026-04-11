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

ローカルの SQLite を使い、Codex や Claude Code など複数の LLM クライアントからセッション横断で記憶を共有する仕組みです。Claude Code では Stop hook で自動保存、Codex にはシェルラッパーを用意しています。DB に書き込めない環境では自動的にファイルキューにフォールバックします。

詳しい使い方は `memory` スキル（`.claude/skills/memory/SKILL.md`）を参照してください。

```bash
make memory-init  # DB 初期化
make memory-demo  # 最小デモ
```
