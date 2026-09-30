# 計画: 共有メモリ基盤の改名（memory-mcp → hikidashi-mcp）への追従

- 着手日: 2026-09-30
- 状態: 実装完了。code-safety-inspector の検証（イテレーション1）で合格
- 分類: 中程度（複数ファイル・既存パターンの名前置換）。tester → implementer → code-safety-inspector。設定値・ドキュメントはオーケストレーターが直接編集
- 前提: [`2026-09-22-memory-mcp-split.md`](2026-09-22-memory-mcp-split.md)

## 背景

共有メモリ基盤のリポジトリ `s4kr4/memory-mcp` を `hikidashi-mcp` へ改名した。それに伴い、環境変数名と MCP サーバー名も変わった。`~/.agents` 側のフック・Codex ラッパー・設定・スキル・README が旧名を参照しているため、これらを追従させる。

| 項目 | 旧 | 新 |
|---|---|---|
| 環境変数 | `MEMORY_MCP_PATH` | `HIKIDASHI_MCP_PATH` |
| CLI / ラッパー | `memory.py` / `run-python.sh` | 変更なし |
| MCP サーバー名 | `shared-memory` | `hikidashi` |
| clone 配置 | `~/worktrees/github.com/s4kr4/memory-mcp` | `~/worktrees/github.com/s4kr4/hikidashi-mcp` |
| Makefile ターゲット | `memory-init` / `memory-demo` / `memory-mcp-check` | `memory-init` / `memory-demo` / `hikidashi-mcp-check` |
| 疎通確認スクリプト | `scripts/memory-mcp-check.sh` | `scripts/hikidashi-mcp-check.sh` |

## 決定事項

| # | 事項 | 決定 |
|---|---|---|
| 1 | 旧変数へのフォールバック | 設けない。`MEMORY_MCP_PATH` だけが設定されている状態は「未設定」として扱う。前提計画の決定 7（位置は1つの環境変数だけで解決し、既定値・フォールバック探索は持たない）を引き継ぐ |
| 2 | 置換の範囲 | MCP サーバー名としての `shared-memory` だけを置き換える。対象はツール名の接頭辞 `mcp__shared-memory__` と TOML のセクション名 `mcp_servers.shared-memory`。スキル名 `shared-memory` と、フックの注意文にある「shared-memory の search」（スキルを指す）は変えない |
| 3 | 既存の plans | 経緯の記録なので書き換えない |

## 完了条件

1. フック2本と Codex ラッパー4本は、`HIKIDASHI_MCP_PATH` だけから CLI の位置を決める。`MEMORY_MCP_PATH` の値は、どの CLI が呼ばれるかに影響しない
2. テストの禁止パターン（clone パスのハードコード、`${HIKIDASHI_MCP_PATH:-…}` による既定値、settings.json への記載）は、新しい名前で検出できる
3. `.claude/settings.json` の許可設定は `mcp__hikidashi__*` になっている（`forget` は ask のまま）。`.codex/config.toml` のセクション名は `[mcp_servers.hikidashi]` になっている
4. README とスキル（`.claude` と `.codex` の両方）に、旧リポジトリ名・旧変数名・旧 MCP サーバー名・旧 make ターゲット名・旧スクリプト名への参照が残っていない

## 変更内容

### A. スクリプト（tester → implementer）

- 対象
  - `.claude/scripts/hook-session-start-philosophy.sh`
  - `.claude/scripts/hook-stop-memory.sh`
  - `scripts/codex-memory-{start,stop,log,run}.sh`
- 内容: `MEMORY_MCP_PATH` を `HIKIDASHI_MCP_PATH` に、ローカル変数 `memory_mcp_path` を `hikidashi_mcp_path` に置き換える。エラーメッセージとコメントも新しい名前にする
- テスト
  - 対象ファイル
    - `scripts/test_hook_session_start_philosophy.py`
    - `scripts/test_hook_stop_memory.py`
    - `scripts/test_codex_memory_scripts.py`
    - `scripts/test-session-start-hooks-config.sh`
  - 新しい名前に揃えるもの: 変数名、`FORBIDDEN_*` の正規表現、おとりの配置先
  - 実データを守る保護パスには、旧 clone のパスも残す
  - 回帰テストを追加する
    - `MEMORY_MCP_PATH` だけを設定した場合は CLI を呼ばない
    - 両方を設定した場合は `HIKIDASHI_MCP_PATH` 側の CLI を呼ぶ

### B. 設定値（直接編集）

- `.claude/settings.json`: `mcp__shared-memory__` を `mcp__hikidashi__` にする
- `.codex/config.toml`: `mcp_servers.shared-memory` を `mcp_servers.hikidashi` にする

### C. ドキュメント（直接編集）

- `README.md` の「共有メモリ」「作業方針の自動注入」節
- `.claude/skills/{memory,shared-memory,memory-extract,linux-diag}/SKILL.md`
  - `.codex/skills/` 側へは `scripts/sync-claude-codex-skills.sh` で同期する

## 🛡️ 想定入力範囲

| 項目 | 内容 |
|---|---|
| 信頼境界 | シェル環境から来る `HIKIDASHI_MCP_PATH` と `MEMORY_MCP_PATH`、フックの stdin |
| 想定する入力の軸 | `HIKIDASHI_MCP_PATH` の値は、既存テストと同じ集合を扱う（未設定・空・相対パス・末尾スラッシュ・空白を含む・存在しない・ディレクトリでない・`memory.py` が無いかディレクトリ・`run-python.sh` が無いか実行できない）。`MEMORY_MCP_PATH` は {未設定, 有効な CLI ツリー, 不正値} × `HIKIDASHI_MCP_PATH` の {未設定, 有効} の組み合わせを扱う |
| 規模の上限 | 既存フックのタイムアウトのまま（変更しない） |
| 実行環境の前提 | 既存テストと同じ（Linux・WSL、bash、jq、coreutils） |
| 想定しない条件 | 旧変数から新変数への自動移行（決定 1） |
| 判定の不変条件 | 呼ばれる CLI は `HIKIDASHI_MCP_PATH` の値だけで決まり、ほかのどの環境変数の値にも依存しない |

## 検証

1. テスト4本がすべて成功する
2. 変異試験で、フォールバックを入れる変異（`${HIKIDASHI_MCP_PATH:-$MEMORY_MCP_PATH}`）がテストで検出される
3. 次の grep の結果を確認する
   - コマンド: `grep -rnI --exclude-dir=.git -E 'memory-mcp|MEMORY_MCP|mcp__shared-memory|mcp_servers\.shared-memory' . | grep -vE '^(\./)?plans/'`
   - 注: 行頭に `./` が付くかどうかは grep の実装によって異なるため、どちらの形でも除外できるようにしている
   - 残ってよいもの: 旧変数を無視することを確かめるテストと、実データの保護パスだけ
4. ステージした状態で `scripts/check-skill-sync.sh` が通る。`jq` で settings.json を、TOML パーサーで config.toml を読み込める

## リポジトリ外の対応（ユーザーが行う）

- シェルの設定ファイルで `export HIKIDASHI_MCP_PATH=<hikidashi-mcp の clone の絶対パス>` を設定する
- Claude Code の MCP 登録を `hikidashi` の名前でやり直す（`claude mcp remove -s user shared-memory` のあと `claude mcp add`）
- Codex 側の MCP 登録名を `hikidashi` にする

## 実施結果

- テスト: unittest 3本で93件、hooks-config で26件、すべて成功。shellcheck と `bash -n` で指摘なし
- テスト設計の縮小: 当初は、旧変数に関する組み合わせ別の振る舞いテストを指示していた。変更の規模に比べて過剰だったため、ユーザー判断で静的検査に絞った。具体的には、既存の `FALLBACK_PATTERNS` に旧変数名と旧 clone パスを1行ずつ加えた
- 変異試験: 6種類の変異（フォールバック・置換漏れ・上書き・コロンなしの既定値を $HOME 配下に置くもの・エラー文言）は、すべて検出された
- 生存した変異（ギャップとして把握済み）:
  - 旧変数名を組み立てて間接参照するもの（`${!_v}`）。静的検査は文字列の一致しか見ないため検出できない。改名で自然に入る形ではない
  - コロンなしの既定値で別の変数へフォールバックするもの（`${HIKIDASHI_MCP_PATH-${OTHER:-}}`）。改名前のパターンから引き継いだ既存のギャップ

## 追加対応: スキル名の変更（2026-10-01）

当初は決定 2 で、スキル名 `shared-memory` を変えない方針にしていた。その後ユーザーの判断で、共有メモリ系の3スキルを基盤の名前と用途に合わせて改名した。

| 旧 | 新 | 用途 |
|---|---|---|
| `shared-memory` | `hikidashi` | 日常の読み書き |
| `memory` | `hikidashi-doctor` | 基盤の設定・構成の変更、障害の診断・復旧 |
| `memory-extract` | `hikidashi-distill` | セッションの生ログから、長く使える意味記憶だけを選んで保存する |

改名前の名前（特に `memory` と `memory-extract`）は、読み書き用のスキルとの役割の違いが名前から読み取れなかった。そこで、`hikidashi-` を共通の接頭辞にして3つが一覧で並ぶようにし、後ろの語で用途を表すことにした。

- 分類
  - スキルディレクトリ（`.claude` / `.codex`）の `git mv` と参照の張り替え（CLAUDE.md・AGENTS.md・README・スキル）は「ドキュメント」として扱った
  - SessionStart フックの注意文にあるスキル名の変更は「単純」として扱った
