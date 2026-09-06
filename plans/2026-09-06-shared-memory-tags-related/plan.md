# 実装計画: 共有メモリのタグ機能と関連記憶

- 作成日: 2026-09-06
- 状態: **実装済み。`@code-safety-inspector` 4 往復合格 2026-09-06。ユーザー側の独立検証（[`verification.md`](verification.md)）の P2 3 件（CRLF 保持、`related` 同点順序の UTC オフセット、CLI `--limit` 範囲）と仕様差 1 件（存在しない related の警告）、および検査員提案の frontmatter 未検出ファイルへの `update_metadata` 早期エラー化は修正・再検証済み。既存記憶へのバックフィルと MCP 再起動後の実機確認は未実施。別タスク: 既存債務（`ruff format` 未整形 2 行、mypy 7 件）、`search` / `get_context` / `_write_index` の `updated` 文字列ソートへの `_updated_sort_key` 適用、`related` 以外の既存 `--limit` の範囲検証**
- 分類: 複雑（多ファイル・ストア層のスキーマ変更・MCP ツール追加）
- 実装方針: `@general-implementer` 1 名が Task 1〜4 を順に実施する（CLI / スクリプト例外により tester/implementer 分離は適用しない）。各タスク内では Red-Green-Refactor を守り、テストを先に書く。完了後に `@code-safety-inspector` で独立検証し、合格時は `/code-review ultra` の実行を提案する。
- 前提計画: [`2026-09-05-shared-memory-mcp.md`](2026-09-05-shared-memory-mcp.md)（MCP サーバー化。実装済み）

## 概要

Markdown Vault の frontmatter に `tags` と `related` を追加し、タグ共有と明示リンクの両面から関連記憶を引けるようにする。MCP ツールを 7 個から 10 個へ増やし（`related` / `list_tags` / `update_metadata` を追加）、`search` にタグ絞り込みを加える。CLI・ドキュメント・スキルも同時に更新する。

## 確定済みの要件（ユーザー選択済み）

1. **タグ共有 + 明示リンク** の両方で関連記憶を引けるようにする
   - frontmatter に `tags:`（文字列リスト）と `related:`（記憶 id のリスト。id は `global/<slug>` や `projects/<project>/<slug>` のディレクトリ付き形式）を追加する
   - 新 MCP ツール `related(memory_id, limit)`: 共有タグ数と明示リンク（双方向）の両方から関連記憶をランキングして返す
   - `search` に `tags` 絞り込みを追加する
2. **既存記憶へのバックフィル**は、実装完了後にオーケストレーターがタグ案を提示→ユーザー承認→MCP `update_metadata(memory_id, tags, related)` 経由で付与する運用とする。コードとしてのバックフィルスクリプトは作らない。調査時点では 13 件であり、実施時に件数を再確認する。通常の upsert でも tags / related を指定できるが、既存記憶のキュレーションには本文の再入力や key の推定が不要な ID 指定更新を使う
3. **タグ語彙は自由 + 正規化 + `list_tags`**
   - 正規化: 小文字 kebab-case。日本語タグを壊さない
   - 新 MCP ツール `list_tags()`: 既存タグと件数を返す。スキル側で「保存前に既存タグを確認して再利用する」手順を追加し語彙のばらつきを抑える

## 調査で確定した前提

- `write()` の frontmatter は `type` / `created` / `updated` の 3 キー固定。`yaml.safe_dump(sort_keys=False)` なので dict の挿入順がそのまま出力順になる。`tags` / `related` は `updated` の後ろに置ける。
- `slugify()` は `[^\w]+` を `re.UNICODE` で潰すため日本語は保持される。ただし `C++` と `C#` がどちらも `c` に潰れる。タグ用には別関数が要る。
- `.claude/settings.json` の permissions allow に `mcp__shared-memory__*` のワイルドカードが既にある。新ツールの個別追加は不要。`ask` の `forget` 個別指定も影響なし。
- ツール数を検証しているのは 2 箇所。`memory/test_mcp_server.py:63` の `test_seven_tools_have_expected_schema` と `memory/check_mcp.py` の `EXPECTED_TOOLS` 定数。
- `local_store.append_observation()` の `value` は任意の dict なので、tags / related を observation 経由で store まで運べる。パイプライン層のスキーマ変更は不要。
- 現在の記憶は 13 件。`projects/linux-diag/` の 4 件は本文中に手書きの `**タグ**:` 行と `## 関連カルテ` セクションを持つ。本文はパースも変更もしない。
- `run_write_memory()` は入力 summary を `summarize_memory()` で加工してから保存する。検索結果の summary を再送すると、例えば `主な OS: Windows, Ubuntu` が `主な OS: 主な OS: Windows, Ubuntu` になる。検索結果は元の key を持たず、既存判定は ID と `humanize_key(key)` によるタイトル一致にも依存するため、バックフィルに通常の書き込み経路を使わない。
- 検証は unittest、実通信試験、Ruff の lint・format check、mypy を行う。コマンドは完了条件に記載する。

## 設計判断

### 1. upsert 意味論

`tags` / `related` は 3 状態を持つ。

- `None`（引数省略）は既存維持。ルールベース自動抽出やタグを意識しない書き込みが既存のタグを消さないため。
- リストは全置換。
- 空リスト `[]` は全消去。専用ツールを増やさずタグ削除を可能にするため。

`None` と `[]` は Python でも JSON でも区別できるので、この 3 状態は MCP 境界を越えても保たれる。CLI では `--clear-tags` / `--clear-related` が明示的に `[]` を渡す。`--tag ""` / `--related ""` は空文字要素として拒否する。各 clear オプションは対応する値指定オプションと排他にする。

### 2. `related` の検証

- 形式検証は書き込み時にハードエラー。`store_paths.validate_memory_id()` をそのまま流用し、裸スラグや `_index` を拒否する。
- 存在検証は警告のみで受理する。`forget` によるアーカイブで dangling 参照は運用上必ず発生し、読み出し側にどのみち耐性が要る。書き込み時だけ厳格にしても整合性は守れず、新規記憶どうしの相互リンクに 2 パスを強いるだけになる。
- 具体的な実装: `upsert_from_observation()`/`update_metadata()` は正規化後の `related` の各 ID について現行記憶として存在するかを確認し、存在しないものがあれば stderr に記憶 ID・フィールド名・対象 ID を含む警告を出す（寛容読み出しの警告と同じ書式）。ただし stderr は MCP 経由の LLM から見えないため、`run_write_memory()`/`run_update_metadata()`（CLI の `write-memory`/`update-metadata`、MCP の `write_memory`/`update_metadata` の両方が通る共通経路）の戻り値に `dangling_related: [...]` を追加し、今回の呼び出しで指定した `related` のうち存在しないものを伝える（存在しなければ空リスト。`related` を指定しなかった呼び出しでは常に空リスト）。
- 読み出し時、`related()` は存在しない参照先を結果から除外し、別フィールド `dangling` に id を並べて返す。黙って消さず、呼び出し側が掃除を判断できるようにする。

### 3. 自己参照・重複・順序

- 自己参照は書き込み時に黙って除去する。
- 重複は除去する。
- tags / related はともに正規化後にソートして保存する。Syncthing 同期する構成では、書き手ごとに順序が揺れると内容が同じでも競合ファイルが生まれる。決定的な並びのほうが差分が安定する。

### 4. タグ正規化

`slugify()` は流用せず、`normalize_tag()` を新設する。仕様は小文字化、前後空白除去、`_` と空白を `-` へ、`\w` と `/` 以外を `-` へ、連続する `-` を 1 個へ、前後の `-` を除去。

- `/` を残すのは Obsidian の階層タグ（`env/wsl` など）を壊さないため。
- 日本語は `\w` に含まれるため保持される。
- 書き込み・検索の入力では、非文字列、空文字、正規化結果が空文字になるタグはエラーにする。`slugify()` の `"untitled"` フォールバックは無意味なタグを黙って生成するのでタグ用途では不適切。
- 既知の制限として `C++` と `C#` は両方 `c` になる。ドキュメントに `cpp` / `csharp` の使用を推奨する旨を書く。

Vault の読み出しは入力検証とは別の寛容な境界とする。

- tags の欠損・YAML null は空リスト。文字列はカンマで分割し、リストは各要素を個別に正規化する。
- 数値・真偽値・辞書などの不正なコンテナは stderr に記憶 ID とフィールド名を含む警告を出し、読み出し結果では空リストとして扱う。リスト内の非文字列・空文字・正規化後に空になる要素も警告してその要素だけ除外する。正常な要素は保持して重複除去・ソートする。
- related も欠損・null は空リスト。不正なコンテナ（文字列を含む）や形式不正な要素は警告して除外する。形式が正しく存在しない ID は保持し、dangling として扱う。
- 読み出しや索引再生成を理由に元ファイルを修復・書き換えない。タグの不正で検索・一覧・索引生成全体を失敗させない。YAML 自体の構文エラーなど、タグ以外の既存エラー処理はこの変更の対象外。

### 5. `related` の値の形式

Obsidian の `[[wikilink]]` ではなく素の id（`global/foo`、`projects/linux-diag/bar`）で保存する。記憶 id は Vault ルートではなく `memory/` からの相対パスなので、`[[global/foo]]` は Obsidian 側で解決できない。素の id なら CLI/MCP の round-trip も自明になる。本文中に手書きで wikilink を置くのは従来どおり自由。

### 6. `updated` を更新するか

タグ・関連の変更では `updated` を更新しない。

- `updated` は本文または type の変更日時で、`score_memory()` の recency ランキングの入力になっている。一括バックフィルで全件の日時を更新すると、本文の鮮度を表すランキング信号が失われる。
- DETAILS.md の既存規則は「本文または type が変わった場合だけ更新」。タグ・関連のみの変更はこれらに当たらない。
- Syncthing の競合検知はファイルの実体と mtime を見るので、frontmatter の `updated` を据え置いても検知能力は落ちない。
- 同じ理由で、タグのみの変更では `## 変更履歴` に行を追加しない。

### 7. `search` の複数タグは AND

指定タグを全て持つ記憶だけを返す。検索は絞り込み操作であり、AND のほうが結果を予測しやすい。OR 相当の「似ているものを広く集める」用途は `related` が担うので機能の重複もない。`tag_mode` のようなモード引数は増やさない。

### 8. `related()` のランキング

明示リンク 1 本につき 3.0、共有タグ 1 個につき 1.0。リンクは向きごとに数えるので、相互リンクは 6.0 になる。同点は `updated` の新しい順、さらに id の昇順で解決する。スコアが 0 の記憶は返さない。`limit` は既定 10、範囲 1〜100 で他ツールと揃える。

戻り値には各ヒットの `score`、`matched_tags`、`link`（`outgoing` / `incoming` / `mutual` / `none`）を含める。なぜ関連と判定されたかが呼び出した LLM に伝わり、結果を鵜呑みにせず取捨選択できる。

タグの希少度による重み付け（IDF 的なもの）は入れない。13 件規模では効果より予測不能性のほうが大きい。将来の選択肢として設計ドキュメントに記録する。

### 9. `_index.md` の表示

タグを持つ記憶の行末に、インラインコードで `` `tags: docker, gpu` `` を付ける。`#tag` 形式は採らない。`_index.md` は全記憶を列挙する生成物なので、`#tag` を書くと Obsidian のタグペインで全タグの件数が二重計上され、タグ検索のたびに `_index.md` がヒットするノイズになる。frontmatter の `tags` で Obsidian のネイティブ機能は既に効いている。

`related` は `_index.md` に出さない。索引の目的は一覧性であり、リンク関係の閲覧は Obsidian のプロパティ表示と `related` ツールが担う。

### 10. frontmatter の最小性

`tags` / `related` が空のときはキー自体を書かない。差分ゼロの保証は、tags / related を持たない既存 Markdown を、本文・type・日時を変えずに読み書きした場合のファイル内容に対するものとする。既存形式を fixture にしてバイト一致を確認する。読み出すだけでは元ファイルを変更しない。CLI/MCP の JSON 出力は tags / related のフィールド追加により変わるため、この保証の対象外。明示的なタグ付与・消去では対象の frontmatter と生成索引が変わる。

### 11. ID 指定のメタデータ更新

`update_metadata(memory_id, tags=None, related=None)` を新設する。通常の `write_memory` の必須引数や summary の加工規則は維持する。

- `memory_id` は `validate_memory_id()` で検証し、既存の現行記憶だけを更新する。存在しない ID や archive の ID はエラーにし、新規記憶を作らない。key・タイトルから ID を再計算しない。
- tags / related は upsert と同じ 3 状態。両方 `None` の場合は入力エラーとし、`[]` は明示的な更新として受理する。
- 既存のストアロック内で検証・読み取り・更新・索引再生成を行う。本文を再組み立てず frontmatter の指定されたフィールドだけを更新し、本文（タイトル・変更履歴・改行を含む）はバイト単位で維持する。type・created・updated・未知の frontmatter キー・省略したフィールドの値も維持する。frontmatter の YAML 表記の再整形は許容する。
- 読み出し時に不正要素を除外した表示用レコードを、そのままファイル全体へ書き戻さない。例えば related のみ更新したとき、未指定の tags を暗黙に修復しない。更新後のファイルが元と同一なら書き込みを省略する。
- CLI/MCP は同じ `run_update_metadata()` を使い、`{"ok": true, "memory": <serialize_memory の結果>}` を返す。本文抽出用のセッション・イベント・observation を作成せず、`summarize_memory()` を通さない。繰り返し呼び出しても本文や履歴に副作用がない。

## 実装ステップ

### Task 1: ストア層（`memory/markdown_store.py`）

**目的**: frontmatter の tags / related を正本として読み書きし、タグ検索・関連検索・タグ集計を提供する。

**成果物**

- `normalize_tag()` / `normalize_tags()` / `normalize_related()` が module レベルに存在する。`normalize_related()` は自己参照除去・重複除去・形式検証・ソートを行う。
- `write()` が非空の `tags` / `related` を `updated` の直後に出力し、空のときはキーを出さない。
- `_read_path()` が設計判断 4 の寛容な読み出し仕様で `tags` / `related` を返す。書き込み用の厳格な検証関数と、警告して不正要素を除外する読み出し処理を分離する。
- `upsert_from_observation()` がキーワード引数 `tags` / `related`（既定 `None`）を受け取り、3 状態の意味論で反映する。既定値付きなので既存呼び出しは無変更で動く。
- `update_metadata(memory_id, *, tags=None, related=None)` を追加し、設計判断 11 の ID 指定更新を実装する。元の frontmatter と本文を保持する更新経路を用意し、通常の `write()` による本文再構築を使わない。
- `search()` が `tags` 引数（AND 絞り込み）を受け取る。
- `related(memory_id, limit)` が上記のランキングで関連記憶を返す。存在しない `memory_id` は明確なエラーとする（CLI/MCP 側で利用者に伝わるように）。
- `list_tags()` が `{tag, count}` を件数降順・同数はタグ名昇順で返す。
- `_index_lines()` がタグ付き記憶にインラインコードのタグ表記を付ける。

**新規テスト（`memory/test_markdown_store.py`）**

- tags / related の round-trip（書いて読んで同一）。
- 空リスト時に frontmatter へキーが出ないこと、既存の tags 無しファイルがそのまま読めること。
- 正規化（大文字、空白、アンダースコア、日本語保持、階層タグの `/` 保持、空になるタグの拒否）。
- upsert の 3 状態（`None` で維持、リストで置換、`[]` で消去）。
- タグのみ変更で `updated` が据え置かれ、変更履歴が増えないこと。
- ID 指定更新の 3 状態、不正 ID・存在しない ID・両方省略の拒否。手編集タイトル・変更履歴・末尾改行を含む本文のバイト一致、type・created・updated・未知の frontmatter キー・未指定フィールドの維持。反復更新で記憶数が増えないこと。
- 不正な tags（`["docker", "!!!", 123, null]`、空文字、数値・真偽値・辞書）と不正な related が警告され、正常な要素と他の記憶は検索・一覧・タグ集計・索引生成に残ること。欠損・null・カンマ区切り文字列の扱いも確認する。
- 不正要素の読み飛ばしだけでは元ファイルが変わらないこと。related のみを ID 指定更新しても、未指定の不正な tags を書き換えないこと。
- 自己参照・重複の除去、ソート順の決定性。
- `search` の複数タグ AND。
- `related` の双方向リンク、相互リンクのスコア、共有タグのみのヒット、dangling の分離報告、limit。
- `list_tags` の集計と並び。
- `_index.md` のタグ表記。

### Task 2: CLI 層（`memory/memory.py`）

**目的**: CLI と MCP が共有する `run_*` 経路にタグ・関連を通す。

**成果物**

- `serialize_memory()` が `tags` / `related` を常に含む（空でも空リストを返し、スキーマを安定させる）。`serialize_history_memory()` にも `tags` を追加し、出力の非対称を残さない。
- `run_write_memory()` が tags / related を observation の `value` に載せ、`upsert_memory_from_observation()` がそれを store へ渡す。値が `None` のときは `value` に載せないことで、既存タグを維持する意味論をパイプライン経路と共有する。
- `validate_write_memory()` が tags / related を検証する。リストであること、各要素が非空文字列であること、正規化後に空にならないこと、related の各要素が `validate_memory_id()` を通ること。MCP の `write_memory` はセッション自動生成の前にこの関数を呼ぶので、不正入力で無駄なセッションが作られない。
- `run_search()` が `args.tags` を store へ渡す。
- `run_related()` / `cmd_related()`、`run_list_tags()` / `cmd_list_tags()` を追加する。
- `validate_update_metadata()` / `run_update_metadata()` / `cmd_update_metadata()` を追加する。タグ・関連の厳格な検証は通常書き込みと共通化し、ID 指定更新は observation 経路を通さずストアへ渡す。不正 ID・不存在・両方省略は CLI/MCP で利用者に伝わるエラーにする。
- argparse: `search` に `--tag`（`action="append"`, `dest="tags"`）、`write-memory` に `--tag`（同上）と `--related`（`action="append"`, `dest="related"`）、新サブコマンド `related`（`--memory-id` 必須、`--limit` 既定 10）と `list-tags`。
- 新サブコマンド `update-metadata` は `--memory-id` 必須、`--tag` / `--related` は `action="append"`。key・summary・session-id は要求しない。
- `write-memory` と `update-metadata` の両方に `--clear-tags` / `--clear-related` を追加する。`--tag` と `--clear-tags`、`--related` と `--clear-related` はそれぞれ argparse の排他グループにする。省略時 `None`、値指定時リスト、clear 指定時 `[]` に変換して共有 `run_*` 経路へ渡す。
- 既存テストが手組みする `argparse.Namespace` を壊さないため、`run_*` 内での新フィールド参照は `getattr(args, "tags", None)` 形式にする。

**新規テスト（`memory/test_memory.py`）**

- `write-memory --tag/--related` から Vault の frontmatter まで届くこと。
- `write-memory` / `update-metadata` の両方で、指定省略による維持、値指定による置換、clear による全消去、値指定と clear の排他エラーを確認する。`--tag ""` / `--related ""` は拒否する。
- `update-metadata --memory-id` が本文・タイトル・日時・履歴を維持し、JSON で更新済み記憶を返すこと。不正 ID・不存在・両方省略の失敗時にファイルや local のデータを変更しないこと。
- `search --tag` の AND 絞り込み。
- `related` / `list-tags` サブコマンドの JSON 出力。
- 不正な related id と空になるタグが `MemoryUsageError` になること。
- `serialize_memory` に tags / related が含まれること。

### Task 3: MCP 層（`memory/mcp_server.py`）

**目的**: 10 ツール構成にし、新機能を MCP クライアントへ露出する。

**成果物**

- `args()` の defaults に `tags=None` / `related=None` を追加する。
- `search` に `tags: list[str] | None = None` を追加する。
- `write_memory` に `tags` / `related` を追加し、`validate_write_memory` へ渡る候補 Namespace にも含める。
- 新ツール `related(memory_id, limit)` と `list_tags()`。どちらも `ToolAnnotations(readOnlyHint=True, idempotentHint=True)`。`related` の `limit` は既存と同じ `Field(strict=True, ge=1, le=100)`。
- 新ツール `update_metadata(memory_id: str, tags: list[str] | None = None, related: list[str] | None = None)`。`ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)` とし、`call("update_metadata", ...)` で CLI と同じ処理へ渡す。セッション自動生成を行わない。
- `check_mcp.py` の `EXPECTED_TOOLS` に 3 ツールを追加する。ハンドシェイク試験にタグ付き `write_memory` → `search` で ID 取得 → `update_metadata` で置換・空リスト消去 → `list_tags` の往復を足す。更新前後の本文・日時の一致も確認する。

**テスト更新（`memory/test_mcp_server.py`）**

- `test_seven_tools_have_expected_schema` を 10 ツール版へ改名・更新。新ツールの annotations、`update_metadata` の必須 ID と nullable なリスト引数、`related` の limit 範囲も確認する。
- タグ付き write_memory → `search(tags=...)` → `related` の in-process 往復を 1 本追加する。
- 既存記憶 fixture を `search` → `update_metadata` でバックフィルし、本文のバイト一致、タイトル・type・created・updated・履歴の維持、記憶数の不変、local のセッション・イベント・observation が増えないことを確認する。一般キー、`primary_os`、key を復元できない手編集タイトルを含める。
- MCP 境界で省略・null・非空リスト・空リストの意味論を検証する。不正 ID・不存在・両方省略・不正タグでエラーが返り、副作用がないことも確認する。

### Task 4: ドキュメントとスキル

**目的**: 仕様と運用手順を実装に追随させる。

- `memory/README.md`: 「7 ツール」の記載を 10 ツールへ。ツール一覧と使用例にタグ・関連・ID 指定のメタデータ更新を追記。CLI の clear オプションも記載する。
- `memory/DETAILS.md`: frontmatter 仕様（`tags` / `related`、空なら非出力、タグ・関連のみの変更では `updated` を更新しない規則）、寛容な読み出しと厳格な入力検証の区別、CLI サブコマンド・clear オプション、ID 指定更新の保持保証を記載。`mcp_server.py` の行は 10 ツールへ更新し、`C++` / `C#` の正規化制限も記載する。
- `memory/llm-shared-memory-design.md`: 「frontmatter の最小化」章に追記として、なぜ `tags` / `related` は最小性方針に反しないか（Obsidian ネイティブのキュレーション情報であり、算術スコアや内部 provenance ではない）、AND 採用理由、ランキング式、IDF を採らなかった理由を記録する。
- `.claude/skills/shared-memory/SKILL.md`: MCP ツール一覧に `related` / `list_tags` / `update_metadata` を追加。保存手順に「保存前に `list_tags` で既存タグを確認し、可能なら再利用する」を追加。既存記憶のタグ・関連のみの変更は検索結果の ID で `update_metadata` を使い、summary を再送しないことを記載する。
- `.claude/skills/memory-extract/SKILL.md`: 抽出時に既存タグを確認してからタグを付ける手順を追加。
- `.claude/settings.json` は変更不要（ワイルドカード許可済み）。この判断もコミットメッセージに残す。

## 影響を受けるファイル

- `memory/markdown_store.py` — 正規化・frontmatter・検索・関連・集計・索引
- `memory/memory.py` — serialize / validate / run_* / argparse
- `memory/mcp_server.py` — ツール追加と引数追加
- `memory/check_mcp.py` — 期待ツール集合とハンドシェイク項目
- `memory/test_markdown_store.py` / `test_memory.py` / `test_mcp_server.py` — 新規テスト
- `memory/README.md` / `memory/DETAILS.md` / `memory/llm-shared-memory-design.md` — 仕様更新
- `.claude/skills/shared-memory/SKILL.md` / `.claude/skills/memory-extract/SKILL.md` — 運用手順

## 注意事項

- tags / related を持たない既存形式の Markdown fixture は、内容を変更せずストアで読み書きしてもバイト一致することを固定する。JSON 出力へのフィールド追加は差分ゼロ保証に含めない。検証には一時ストアを使い、実 Vault の全件書き戻しは行わない。
- `projects/linux-diag/` の 4 件は本文に手書きの「**タグ**:」行と「## 関連カルテ」がある。本文は一切パースせず触らない。frontmatter を後付けするだけ。
- 手編集された `tags: docker` のような文字列形式を読み落とすと、Obsidian で付けたタグが黙って消える。読み出しの寛容さは必須。
- `related` の dangling は正常状態として扱う。エラーにしない。
- `related` という語が CLI サブコマンド名、frontmatter キー、argparse の dest で重なる。store のメソッドは `related()`、引数フィールドは `args.related`（書き込み用）と `args.memory_id`（照会用）で役割を分ける。

## 完了条件

- [ ] タグを 2 つ付けて `write_memory` した記憶のファイルを開くと、frontmatter に `updated` の次へ `tags` が小文字 kebab-case のソート済みリストで現れる
- [ ] タグを付けずに保存した記憶のファイルには `tags` キーも `related` キーも現れない
- [ ] `search` にタグを 2 つ渡すと、その両方を持つ記憶だけが返る
- [ ] `update_metadata` に検索結果の ID とタグ・関連だけを渡すと、本文全体がバイト一致し、タイトル・type・created・updated・履歴が維持され、記憶数が増えない
- [ ] `update_metadata` は省略・null のフィールドを維持し、非空リストで置換し、`[]` で全消去する。不正 ID・不存在・両方省略・不正タグを副作用なしで拒否する
- [ ] `update_metadata` は本文抽出用のセッション・イベント・observation を作成せず、同じ更新の反復でも本文・履歴が変わらない
- [ ] `write_memory` をタグ指定なしで再実行すると、既存のタグが残る
- [ ] `write_memory` にタグの空リストを渡すと、frontmatter から `tags` キーが消える
- [ ] `write-memory` / `update-metadata` の `--clear-tags` / `--clear-related` で全消去でき、対応する値指定との併用と空文字要素は拒否される
- [ ] 不正なタグ・関連要素は読み出し時に警告され、正常な要素と他の記憶は検索・一覧・索引生成に残る。読み出しだけでは元ファイルを変更しない
- [ ] related のみの ID 指定更新で、未指定の tags（不正値を含む）や未知の frontmatter キーを変更しない
- [ ] A が B を `related` に持つとき、`related("B")` の結果に A が `link: "incoming"` 付きで含まれる
- [ ] 相互リンクの相手は、共有タグ 1 個だけの記憶より上位に並ぶ
- [ ] `related` の参照先を `forget` した後に `related` を呼ぶと、結果本体には現れず `dangling` に id が並ぶ
- [ ] 自分自身の id を `related` に指定して保存しても、frontmatter に自己参照が残らない
- [ ] `list_tags` を呼ぶと、各タグと件数が件数降順で返る
- [ ] 日本語のタグを保存して読み戻すと文字が壊れていない
- [ ] `search --tag`、`write-memory --tag/--related`、`update-metadata`、`related`、`list-tags` が CLI から JSON を返す
- [ ] MCP のツール一覧が 10 個になり、`check_mcp.py` が成功する
- [ ] `uv run --locked --project memory python -m unittest discover -s memory -p "test_*.py"` が全通過する
- [ ] `make memory-mcp-check` が成功する
- [ ] `uv run --locked --project memory ruff check memory` が成功する
- [ ] `uv run --locked --project memory ruff format --check memory` が成功する
- [ ] `uv run --locked --project memory mypy --config-file memory/pyproject.toml memory` が成功する
- [ ] tags / related を持たない既存形式の Markdown fixture を内容不変で読み書きしてもバイト一致する。JSON は新フィールドを含む仕様で別途検証する

## 除外範囲

- 既存 13 件へのバックフィル用スクリプト（運用で対応、下記参照）
- 本文中の「**タグ**:」行や「## 関連カルテ」の自動パース・自動移行
- タグの改名・統合ツール、タグの別名辞書
- IDF などタグ希少度による重み付け
- `search` のタグ OR モード、タグの前方一致・階層タグの親子解決
- `history` へのタグ絞り込み追加（`serialize_history_memory` への出力追加のみ行う）
- `_index.md` への `related` 表示

## 実装後の運用手順（オーケストレーター担当）

1. 実装がマージされたら、MCP サーバープロセスを再起動し、接続先で `related` / `list_tags` / `update_metadata` を含む 10 ツールが見えることを確認する。
2. `list_tags` を呼び、現在のタグを確認する。未付与なら空だが、既に付与されたタグがあれば再利用する。
3. `search(query="", limit=100)` で現行記憶を列挙し、`get_context` で必要な文脈を補う。調査時の 13 件を固定値として扱わず、実施時の件数と列挙範囲を確認する。上限に達した場合は scope / project_id で分割して取得し、ID で重複除去して漏れを確認する。各 ID へのタグ案と related 案を一覧にしてユーザーへ提示する。
4. ユーザー承認後、MCP の `update_metadata` に一覧の `memory_id` と承認済みの `tags` / `related` だけを渡す。全置換なので維持する既存値も案に含める。変更しないフィールドは省略し、消去を承認されたフィールドだけ `[]` を渡す。key の推定や summary の再送は行わない。更新前後で本文・タイトル・日時が変わらず、記憶数が増えていないことを確認する。
5. 付与後に `list_tags` で語彙の揺れ（同義タグの併存）を確認し、必要なら統合をユーザーへ提案する。
6. `projects/linux-diag/` の 4 件は、本文の手書きタグ行と「## 関連カルテ」を出典としてタグ案・related 案を組み立てる。本文は書き換えない。

## 重要な決定事項（実装者への引き継ぎ要点）

1. タグのみの変更で `updated` を更新しない
2. 複数タグは AND
3. `related` の存在検証は警告のみ
4. 空なら frontmatter にキーを書かない
5. バックフィルは `update_metadata` の ID 指定更新で行い、本文を再入力しない
6. CLI の全消去は clear オプションで明示する
7. 不正なタグ・関連は入力時に拒否し、Vault 読み出し時は警告して除外する
8. 差分ゼロは内容不変の既存 Markdown に対する保証であり、JSON の新フィールド追加とは区別する
