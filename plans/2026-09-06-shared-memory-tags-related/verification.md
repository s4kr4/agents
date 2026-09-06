# 共有メモリのタグ・関連機能：実装検証結果

- 検証日: 2026-09-06
- 対象計画: [2026-09-06-shared-memory-tags-related.md](../2026-09-06-shared-memory-tags-related.md)
- 比較元: `22a8d01`（検証時の HEAD）
- 検証対象: 検証時点の未コミット実装差分。以後の変更には再検証が必要
- 判定: **修正が必要な問題3件と、計画との仕様差1件あり**
- 検証に伴う実装ファイルの変更: なし

## 修正が必要な問題

### 1. [P2] update_metadata が本文の CRLF 改行を LF に変更する

- 場所: `memory/markdown_store.py:932`（行番号は検証時点）
- 原因: `Path.read_text()` が改行を自動変換する。取得した本文をそのまま使っても、書き戻し時には CRLF が LF になる。
- 影響: Windows で編集された記憶にタグを付けるだけで本文全体に差分が生じる。計画の「本文をバイト単位で保持」に反する。

一時ストアに CRLF の Markdown を配置し、`update_metadata(memory_id, tags=["shared"])` を呼び、更新前後の本文をバイト列で比較した。

```text
更新前: b'\r\n# CRLF\r\n\r\nBody\r\n'
更新後: b'\n# CRLF\n\nBody\n'
本文のバイト一致: False
```

修正方針:

- 本文を改行変換しない方法で読み書きする。frontmatter の区切り処理でも CRLF を扱う。
- CRLF・混在改行を含む fixture を追加し、`read_bytes()` で本文を比較する。
- 現在の保持テストは `read_text()` 同士を比較するため、改行の変化を検出できない。

### 2. [P2] 関連検索の日時順序が UTC オフセットの違いで逆転する

- 場所: `memory/markdown_store.py:884`
- 原因: 同点時の `updated` を文字列として降順に比較している。
- 影響: UTC オフセットが異なる記憶では「updated の新しい順」にならない。

同じ共有タグを持つ、同スコアの記憶2件で次の順位を再現した。

| 実際の順位 | ID | updated | UTC 換算 |
|---|---|---|---|
| 1 | `global/older` | `2026-09-06T10:00:00+09:00` | 01:00 |
| 2 | `global/newer` | `2026-09-06T02:00:00+00:00` | 02:00 |

新しいのは `global/newer` なので、期待順位は逆。

修正方針:

- 日時として解釈して比較する。
- 異なるオフセット、同一時刻の表記違い、既存の日付のみの値を含むテストを追加する。
- スコア降順・日時降順・ID 昇順の優先順位を維持する。

### 3. [P2] CLI の related --limit が範囲外の値を受理する

- 場所: `memory/memory.py:1039`、`memory/markdown_store.py:888`
- 原因: MCP には `1〜100` の検証があるが、CLI・ストアにはなく、そのままスライスに渡している。
- 影響: 呼び出し経路で入力仕様が異なり、負数が「末尾から除外」という別の意味になる。

一時ストアに関連記憶3件を用意し、実際の CLI 子プロセスで確認した。

| 指定 | 終了コード | 結果 |
|---|---|---|
| `--limit -1` | 0 | 2件を返す |
| `--limit 0` | 0 | 0件を返す |
| `--limit 101` | 0 | 3件を返す |

修正方針:

- CLI/MCP 共通処理、またはストアで `1〜100` を検証する。
- CLI 経由で範囲外を拒否し、境界値 1・100 を受理するテストを追加する。

## 計画との仕様差

### 存在しない related ID を保存しても警告が出ない

計画の設計判断2は「存在検証は警告のみで受理する」。実装と `memory/DETAILS.md` は「存在検証しない」としており、仕様が一致していない。

一時ストアで `related=["global/missing"]` を指定し、stderr を捕捉して確認した。

| 経路 | stderr |
|---|---|
| `upsert_from_observation()` | 空文字列（警告なし） |
| `update_metadata()` | 空文字列（警告なし） |

参照先が存在しなくても受理する方針は維持し、警告を追加するか、警告しない設計へ計画を明示的に揃える必要がある。

## 自動検証結果

| 検証 | 結果 |
|---|---|
| unittest | **386件成功** |
| `make memory-mcp-check` | **成功、10ツール確認** |
| Ruff lint | **成功** |
| Ruff format check | 未整形2箇所、1ファイル |
| mypy | 2ファイルで7エラー |
| `git diff --check` | **成功** |

実行コマンド:

```bash
uv run --locked --project memory python -m unittest discover -s memory -p "test_*.py"
make memory-mcp-check
uv run --locked --project memory ruff check memory
uv run --locked --project memory ruff format --check memory
uv run --locked --project memory mypy --config-file memory/pyproject.toml memory
git diff --check
```

### 実行環境による失敗と再実行

通常の uv キャッシュ `/home/s4kr4/.cache/uv` が読み取り専用で、権限拡張後も同じエラーになった。`UV_CACHE_DIR=/tmp/shared-memory-review-uv-cache` を指定して再実行した。

全テストではロックの既定保存先 `/home/s4kr4/.cache/llm-memory/locks` も読み取り専用だった。この段階の結果は386件中4 failures・238 errors。権限拡張後も解消しなかったため、`XDG_CACHE_HOME=/tmp/shared-memory-review-cache` も指定して再実行し、386件すべて成功した。これらの初期失敗は実装不具合の判定に含めない。

追加再現確認は一時ストアと隔離した CLI 保存先で実施した。`make memory-mcp-check` は既存スクリプトが用意する一時 Vault/local/queue/config/cache を使用した。

### 変更前から存在する静的検査の問題

比較元 HEAD の Python ファイルと設定を一時ディレクトリへ取り出し、同じ環境の Ruff・mypy で比較した。

| 検査 | 変更前 HEAD | 検証対象 |
|---|---|---|
| Ruff format | `mcp_server.py` の2箇所 | 同じ2箇所 |
| mypy | 2ファイルで13エラー | 2ファイルで7エラー |

未整形箇所は `forget` と `mark_extracted` のデコレーター。現在の mypy 7件は以下で、いずれも変更前から存在する。

- `test_memory_config.py:24`: `importlib.util` の属性認識（1件）
- `test_mcp_server.py:61`: ContentBlock の型を絞らず `.text` を参照（4件）
- `test_mcp_server.py:178`: `Tool | None` のまま `.fn` を参照（1件）
- `test_mcp_server.py:208`: 環境変数用マッピングへ文字列以外を含む辞書を渡す型不一致（1件）

今回の差分で新たに生じた静的検査エラーとは判定しないが、全体の format・mypy が成功しているわけではない。

## 再検証事項と未実施範囲

- 上記3件の修正後、回帰テストと全テスト・静的検査・MCP 実通信試験を再実行する。
- 存在しない related ID の警告方針を計画・実装・ドキュメントで統一する。
- 実クライアントの再起動後の確認、実 Vault へのバックフィルは今回の検証では実施していない。
- 検証結果はこの時点の実装に対するものであり、修正済みとは扱わない。
