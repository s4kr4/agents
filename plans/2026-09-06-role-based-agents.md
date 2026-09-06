# 実験: エージェント分割軸を「ドメイン」から「役割」へ

- 作成日: 2026-09-06
- 状態: **導入済み（2026-09-06）。ワークフロー・tdd スキル・ルール・他エージェント定義は `@tester` / `@implementer` のみを参照する。旧 5 定義はユーザー判断で削除（git 履歴から復元可能）**
- 根拠: `~/.agents/CLAUDE.md` の「エージェント構成の見直し方針」「削減の実験手順」

## 背景

`.claude/agents/` の tester / implementer は web-api / web-ui / general のドメイン別に 5 定義あり、tester は web 側にしか存在しなかった。そのため CLI・スクリプト・ライブラリの実装は `@general-implementer` がテストと実装を単独で書く例外運用になっていた。

2026-09-06 の共有メモリのタグ機能実装（[`2026-09-06-shared-memory-tags-related/`](2026-09-06-shared-memory-tags-related/)）で、この例外の実害が確認された。implementer が書いた fixture はすべてストアの正規形（LF・末尾改行 1 つ）で、計画が保証した「本文をバイト単位で保持」に対して手編集ファイル・CRLF を検証するテストがなく、末尾改行欠落と CRLF 変換の 2 バグが自動テストを素通りした。CLI の `--limit` 範囲や UTC オフセットの同点順序も実装者のテストに含まれず、いずれも外部の独立検証が検出した。

## 判断

- 「web / 非 web」の分割が変えているのは事前読み込みスキルとモデル選択だけで、手順・出力形式・引き継ぎはほぼ同一だった（`web-api-implementer.md` と `general-implementer.md` の差分で確認）。これは `CLAUDE.md` の分類でいう「能力の補完」であり、モデル更新で不要になる
- tester / implementer 分離は「検証の独立性」であり、原則維持する。CLI 例外はこの原則に反していた
- したがって分割軸を役割に変え、ドメイン差はスキル指定で吸収する

## 変更内容（このコミット）

- 新設: `.claude/agents/tester.md`、`.claude/agents/implementer.md`（ドメイン非依存。ドメイン固有スキルは委譲プロンプトで指定し、エージェントが `Read` で読む）
- `~/.claude/rules/development-workflow.md`: Phase 3a / 3b とパターン 1〜6、フィードバックループ、Related Resources を `@tester` / `@implementer` に統一。CLI 例外の記述は除去し、分離を non-web にも適用する理由を注記
- `~/.claude/skills/tdd/SKILL.md`: 早期判定と呼び分け表を役割ベースへ、fixture に非正規形入力を含める指針を追加
- `~/.claude/rules/tdd.md`: 例外の適用条件を `@tester` / `@implementer` に変更
- 旧定義（`web-api-*`、`web-ui-*`、`general-implementer`）はユーザー確認のうえ削除した（`.codex/skills/general-implementer/` の Codex 側ミラーは同期スクリプトの対象外のため別途整理）

## 評価基準（削減の実験手順 2〜4）

普段の代表的なタスク 2〜3 件を `@tester` → `@implementer` で実施し、次を旧構成と比較する。

| 観点 | 計測方法 |
|---|---|
| 検証フェーズの指摘件数 | `@code-safety-inspector` の不合格項目数と、ユーザー側の独立検証で追加検出された件数 |
| 差し戻し回数 | tester ⇔ implementer の差し戻し回数 |
| 手戻りの有無 | イテレーション上限到達・エスカレーションの有無 |
| ドメインスキルの取りこぼし | 委譲時のスキル指定漏れで規約違反が出た件数 |

劣化がなければ旧 5 定義を `.bak` へ退避し、`development-workflow.md` と `tdd` スキルから「旧」表記を除去する。劣化があれば旧定義へ戻し、原因（スキル指定漏れ・モデル差など）を記録する。

## 未決事項

- `@web-ui-verifier` は役割（ブラウザ検証）で分かれているため対象外
- `code-planner.md` の tools にある `EnterPlanMode` / `ExitPlanMode` はサブエージェントでは使えない。別途整理する
- 旧定義の Serena 設定（`mcpServers: serena`）は `implementer.md` に引き継いだ。Serena 未対応言語では `Edit` にフォールバックする旨を明記した
