# 実装記録: 共有メモリ基盤を memory-mcp リポジトリへ分離する

- 着手日: 2026-09-15 / 完了日: 2026-09-22
- 状態: **完了。`~/.agents` 側は `memory/` と memory 専用 scripts を削除済み。memory-mcp は private リポジトリとして GitHub へ push 済み**
- 分類: 複雑（多ファイル・リポジトリ構成の変更）
- 実装方針: 移動と実装を別コミットに分け、移動は `git mv` で rename として履歴に残す。各実装は失敗するテストを先に追加してから進める。
- この文書は経緯・判断・破棄した案を残すためのもの。運用ドキュメント（README・スキル）には現行構成のみを書く方針のため、切り替え前の構成はここだけに記録する。

## 背景と目的

共有メモリ基盤（旧 `~/.agents/memory/` 配下の CLI・ストア・MCP サーバーと、`scripts/` の memory 専用スクリプト）を、単体リポジトリ `s4kr4/memory-mcp`（private）へ切り出す。

`~/.agents` はエージェント定義・ルール・スキルの配布物であり、共有メモリ基盤はそれとは独立した保守単位になっていた。同じリポジトリに同居していたため、基盤側の変更がエージェント設定の履歴に混ざり、基盤単体での配布・バージョン管理ができない状態だった。

## 決定事項

| # | 事項 | 決定 |
| --- | --- | --- |
| 1 | 参照方式 | 独立 clone + パス更新。submodule・symlink は不採用 |
| 2 | 履歴 | git-filter-repo で保持する。元リポジトリの複製に対して実行し、元は不変とする |
| 3 | memory 専用 scripts | `deploy-memory-mcp.sh` / `.ps1`、`memory_mcp_config.py`、`memory-mcp-check.sh` とそのテストは memory-mcp 側へ移設 |
| 4 | GitHub | private。push はユーザーが手動で行う |
| 5 | 実データ | `local/`・`memory.db` は clone 直下へ移動 |
| 6 | クライアント連携 | SessionStart フック・Stop フック・Codex ラッパーは **`~/.agents` の責任として残す** |
| 7 | CLI の位置解決 | 環境変数 `MEMORY_MCP_PATH` のみで解決する。既定値・フォールバック探索は持たない |

決定 6 は作業の途中で方針を変更したもの。当初はこれらも memory-mcp 側へ移す計画だった。

## 破棄した案

**フック本体を memory-mcp の clone 直下に置き、`.claude/settings.json` から clone のパスを直接指す。**

「memory 関連のものは memory-mcp に集める」という素朴な分け方だったが、ユーザーの指摘により「フックを呼ぶのは `~/.agents` の責任であり、呼ばれる CLI が memory-mcp の責任」と整理し直した。分離の境界は機能の話題ではなく責任の所在で引くべきだった。

既存フックがすべて `.claude/scripts/hook-*.sh` にある作法とも揃い、`settings.json` がリポジトリ外の絶対パスを持たなくなる利点もある。

## 実施内容（`~/.agents` 側）

| コミット | 内容 |
| --- | --- |
| `51663ed` | フック 2 本と Codex ラッパー 4 本、テスト 1 本を `memory/` から `.claude/scripts/`・`scripts/` へ `git mv`。中身は無変更。rename として履歴を残すため実装と分離した |
| `df52289` | 6 スクリプトが `MEMORY_MCP_PATH` のみで CLI を解決するよう実装。テスト 87 件（スキップ 0 で実行するには `jq` が必要） |
| `cd03db3` | `.claude/settings.json` と README を新構成へ切り替え |
| `4077844` | スキル 4 つ（memory / shared-memory / memory-extract / linux-diag）の参照を `$MEMORY_MCP_PATH` 基準へ |
| （本コミット） | `memory/` と memory 専用 scripts の削除、Makefile・README・.gitignore の整理、本文書の追加 |

## 実施内容（memory-mcp 側）

- git-filter-repo で `memory/` をルートへ平坦化しつつ履歴を抽出（`--path-rename memory/:`）
- ルートへの平坦化で壊れる箇所を修正した。`codex-memory-*.sh` が `../memory` を前提にしていた点と、テストの `repo_root` がディレクトリ名 `memory` に依存していた点
- CLI の出力形式（`search` の JSON 形状、`write-memory` の `"<key>: <value>"` summary）を境界契約としてテストで固定してから、移設済みのフック・ラッパーとそのテストを削除した。削除を先にすると境界の振る舞いが無検証のまま残る

## 検証で見つかった主な事項

- `LocalPipelineStore` はコンストラクタで保存先を mkdir する。そのため検索しかしないフックでも clone 側に空の `local/` が作られる。データ移動では `mv -T` を使い、移動先が空でなければ失敗して止まるようにした（入れ子で `local/local/` を作ることの防止）
- 変異試験で 2 つのテストの穴が見つかり、回帰テストを追加して塞いだ
  - `run.sh` 自身のガードが `start.sh` のガードに隠れて検出できない
  - SessionStart フックの `set -e` 不使用が未保護

## 残課題

- SessionStart フックは stdin を閉じて起動されると `cat: -: Bad file descriptor` を stderr に出す（切り替え前からの既存動作。実際の呼び出し元では非到達）。`cat >/dev/null 2>&1` で抑止し、テストの stderr 非制約を解除する対応は、ユーザーが「切り替え完了後に別途」と判断した
- `MEMORY_MCP_PATH` は対話シェルでのみ定義されるため、対話シェルを経由しない起動には届かない（`LLM_MEMORY_VAULT` も同じ挙動）。届かない場合は注意文へフォールバックする
- MCP 登録の `LLM_MEMORY_LOCAL_DIR` が存在しない場所を指していても、ストアが保存先を自動で作るためエラーにならず、静かにデータが分岐しうる（memory-mcp 側の将来課題）

## ロールバック手順

1. `~/.agents` 側は各コミットの `git revert`
2. memory-mcp 側も同様（GitHub へ push 済みのため revert で戻す）
3. 実データは `mv -T` で元の位置へ戻す
4. MCP 登録は `claude mcp remove -s user shared-memory` のうえ `claude mcp add` をやり直す
5. `~/.codex/config.toml` は `.bak` から復元する
