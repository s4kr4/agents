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
├── .codex/                # Codex 側の設定（.claude と相互に同期）
│   ├── AGENTS.md          # Codex 向け開発ガイドライン
│   ├── config.toml        # Codex 設定
│   ├── skills/            # スキル定義（Claude 側のミラー）
│   └── rules/             # 開発ルール・ガイドライン
├── .githooks/             # リポジトリ共有の Git フック（既定では無効）
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

## Codex の残量表示

`scripts/codex-rate-status` は、Claude 版と同じ形式で 5 時間・7 日の残量を表示します。Python 3.11 以降の標準ライブラリのみを使用し、実行時の `PATH` に `python3` が必要です。

```bash
./scripts/codex-rate-status
# 5h:79% 7d:94%
```

`make deploy` で個別の残量コマンドと herdr 専用の `agent-rate-status` を `~/.local/bin/` にシンボリックリンクとして配置します。個別の `claude-rate-status`・`codex-rate-status` は残量のみを出力します。

herdr の表示用コマンドには `agent-rate-status` を指定します。`⚡ Claude 5h:79% 7d:94% | Codex 5h:50% 7d:75%` のように、左にアイコンを 1 つだけ付けて集約します。herdr サーバーの `PATH` 上で `claude`・`codex` の存在を確認し、対応する残量コマンドが成功して値を返した項目だけを表示します。CLI がない場合やデータが空の場合はラベルごと非表示になり、両方とも表示できなければ無出力です。CLI 自体は起動しません。`PATH` には各 CLI と `~/.local/bin` が必要です。

情報源は `${CODEX_HOME:-$HOME/.codex}/sessions/*/*/*/*.jsonl` の `event_msg` → `token_count` → `rate_limits` です。各セッションの最後に追記された有効な Codex の記録を候補とし、候補間で記録日時が最新のものを表示します。使用率を四捨五入して 100 から引いた値が残量です。両枠が揃った有効な記録がない場合は無出力で正常終了し、記録から 15 分を超えた値には末尾に `*` が付きます。

ローカル記録の形式に依存するため、Codex の更新で追従が必要になる場合があります。通信やファイル書き込みは行わず、表示は最後に記録された値です。同じ保存先でアカウントを切り替えた場合は区別できません。

## Git フック

`.githooks/pre-commit` は、`.claude/` と `.codex/` のスキル・エージェントが同期しているかをコミット前に検査します。**既定では有効になっていません。** 有効化すると、このリポジトリのコミット時にフックが実行されるようになります。

```bash
make hooks-install    # フックを有効化（core.hooksPath=.githooks）
make hooks-uninstall  # フックを無効化
```

有効化した状態で片側だけをステージしてコミットすると、フックが不足している側を自動で同期してステージに追加します（`--auto-sync` を渡しているため）。

1 回だけ検査を迂回したい場合は、環境変数を付けて実行します。

```bash
SKIP_SKILL_SYNC_CHECK=1 git commit -m "..."
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

## 作業方針の自動注入

共有メモリの `philosophy` タグに保存した記憶を、SessionStart フック（`memory/hook-session-start-philosophy.sh`）がセッション開始時・`/clear` 後・コンテキスト圧縮後に自動注入します。

対応環境は Linux・WSL・macOS です。ネイティブ Windows には配布していません（配布は bash 版の `deploy.sh` のみで、PowerShell 版の導入スクリプトは MCP のみを扱います）。実行には bash・jq・GNU coreutils の `timeout`（macOS では Homebrew の `gtimeout`）が必要です。macOS では事前に `brew install jq coreutils` を実行してください。

Claude Code は `.claude/settings.json` の `SessionStart` に登録済みで、`make deploy` でそのままデプロイされます。

Codex はリポジトリで管理せず、`~/.codex/hooks.json` の `hooks.SessionStart` 配列に、次のエントリを手動で追記します。

```json
{
  "matcher": "^(startup|clear|compact)$",
  "hooks": [
    {
      "type": "command",
      "command": "/home/<ユーザー名>/.agents/memory/hook-session-start-philosophy.sh",
      "timeout": 10,
      "statusMessage": "作業方針を読み込み中..."
    }
  ]
}
```

追記後は Codex の `/hooks` で信頼を承認してください。定義を変更した場合は再承認が必要です。このリポジトリに `.codex/hooks.json` を置くと、プロジェクト層としても読み込まれ二重注入になるため置きません。

注入される各行には出所を確認できるよう記憶の id を `[id]` の形式で併記します（例: `- 小さな変更を優先する [global/philosophy-minimal-change]`）。

行や id の偽装を防ぐため、注入前に summary の改行と ASCII の制御文字（U+0000〜U+001F、U+007F）、Unicode の行区切り（U+0085、U+2028、U+2029）を空白に置き換えて1行に整形します。id に `[`・`]`・空白・制御文字を含む記憶、および summary が空または空白のみの記憶は注入対象から除外し、省略件数に数えます。取得した応答が期待する型（`memories` が配列で、各要素の `id`・`summary` が文字列）を満たさない場合は、注意文を注入します。

取得と本文の組み立ては、どちらも `LLM_MEMORY_HOOK_TIMEOUT`（1〜8 の整数、既定5秒）の時間制限の内側で行います。範囲外・非整数の値は既定にフォールバックします。本文の組み立ては2,000字の上限で頭打ちになるため、記憶の件数が増えても時間は伸びません。Claude Code 側の hook timeout（10秒）より必ず短くしてあり、外側のタイムアウトで打ち切られて処理が孤児化するのを防ぎます。

起動時に stdout・stderr へ出力する `BASH_ENV` 等のシェル設定があると、注入する JSON が壊れます。フックを使う端末ではそうした設定を避けてください。
