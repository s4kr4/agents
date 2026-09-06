---
name: shared-memory
description: Claude Code と Codex の通常の作業で共有メモリを読み書きする。ユーザーが覚えておいて、記録しておいて、前回の作業、共有メモリなどを求めたとき、または過去の設定・判断が現在の作業に影響するときに使う。
---

# Shared Memory

通常の作業で共有メモリを参照・保存するためのスキル。会話全文や一時的な作業ログは保存しない。

## 作業開始時

過去の好み、制約、判断が現在の作業に影響する可能性がある場合、最初に `get_context(project_id="対象プロジェクト")` を呼ぶ。必要な語句が決まっている場合は `search`、経緯が必要な場合は `history` を使う。

## 作業中・完了時

将来の判断を変える確認済みの知見だけを短く候補にする。完了時に既存記憶との重複・矛盾を確認し、保存する場合は `write_memory` で要約する。保存前に `list_tags` で既存タグを確認し、同義のタグを増やさず可能な限り再利用する。

保存する対象は、ユーザーの恒常的な好み、開発環境・ツール設定、繰り返し参照するプロジェクトの決定事項、協働上の確認済みの知見。会話全文、一時的な作業内容、未検証の推測、リポジトリに既に書かれている事実は保存しない。

## MCP ツール

- `get_context`: 現在の応答に使う文脈を取得
- `search`: 現行記憶を検索。`tags` を渡すと、指定した全タグを持つ記憶だけに絞り込む（AND）
- `history`: 記憶・セッション・イベントの経緯を検索
- `write_memory`: 確認済みの知見を保存。`tags`（タグのリスト）・`related`（関連記憶の `id` のリスト）を任意で指定できる。省略時は既存のタグ・関連を維持し、空リスト `[]` を渡すと全消去する
- `related`: 指定した記憶に共有タグ・明示リンクで関連する記憶を探す。各ヒットに `score`・`matched_tags`・`link`（`outgoing`/`incoming`/`mutual`/`none`）が付き、参照先が撤回済みの場合は `dangling` に分離される
- `list_tags`: 既存のタグと件数を件数降順で取得する
- `update_metadata`: 既存記憶のタグ・関連だけを本文を変えずに更新する。`memory_id` は必須、`tags`/`related` は省略可（両方省略はエラー）。既存記憶のタグを直すだけのときは、summary を再送する `write_memory` ではなくこちらを使う
- `forget`: ユーザーが明示した記憶を archive へ移動。`memory_id` は `search`/`history`/`get_context` が返す `id`（`global/<slug>` や `projects/<project>/<slug>` などディレクトリ階層を含む形式）をそのまま渡す。ディレクトリを省いた裸のスラグは拒否される

`write_memory` の日常保存では `session_id` を省略できる。`scope="project"` では `project_id` を指定する。`memory-extract` が元セッションから抽出するときだけ、元の `session_id` を必ず渡す。

保存形式、保存先、CLI フォールバック、接続設定は [`memory/README.md`](../../../memory/README.md) を参照する。MCP の起動・権限・保存エラーは `memory` スキルで診断する。
