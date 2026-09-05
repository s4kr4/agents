# 実装計画: 共有メモリ基盤の MCP サーバー化

- 作成日・レビュー反映日: 2026-09-05
- 状態: **実装済み。独立検証（@code-safety-inspector、4 往復）合格 2026-09-06。実機の Windows/macOS/GUI 検証と個人設定への適用は未実施**
- 分類: 複雑（多ファイル・アーキテクチャ変更）
- 実装方針: 各振る舞いの失敗するテストを先に追加し、実装・回帰確認へ進む。独立レビューは実装後に実施する。
- この文書の更新は計画へのレビュー対応。クライアント設定への適用や実装の完了を意味しない。

## 概要と達成範囲

`memory/` の処理を CLI とローカル stdio MCP サーバーの両方から利用可能にする。保存先の解決を共通化し、端末ごとの絶対パスを使ったクライアント設定を生成する。既存 CLI・シェルラッパー・Makefile ターゲットの正常系を維持する。

本計画は**各端末への導入・権限設定の共通化と自動化**を対象とする。全端末が接続する単一サービスへの集約ではない。端末ごとの Vault 設定と各アプリの接続・ツール承認は残る。起動コマンドの形式は共通化するが、絶対パスは端末ごとに異なる。

前段で提案された常時稼働サービスへの集約は別案として残す。オフライン利用と既存 Syncthing 運用を維持するため、この計画では stdio を採用する。ただし、端末間の同時編集を競合なしで保証する必要が生じた場合は、単一の書き込みサービスへ設計を見直す。

## 確認済みの事実と未検証事項

- `memory.py` の `cmd_*` は stdout に JSON を出し、一部は入力エラーで `SystemExit` を送出する。stdio MCP へ直接流用しない。
- `cmd_*` の既存テストは `argparse.Namespace` を手組みする。CLI の出力形状を維持したまま `run_*` を抽出できる。
- `local_store.py` の未抽出一覧は summary のあるセッションだけを対象にする。自動セッションは summary を作らず一覧へ混ぜない。
- 現行の ID→パス変換に保存領域内の検証がない。一時領域で `mark_extracted('../outside')` が sessions 外の JSON を更新することを確認した。
- atomic write の一時ファイルには UUID がある。問題は一時名の衝突ではなく、read-modify-write・索引更新・archive 先決定の排他不足。
- `run-python.sh` は明示 Python、PyYAML 入りシステム Python、uv の順に選ぶ。uv 経路だけ変えても CLI と MCP の環境は統一されない。
- `scripts/deploy.sh` の既定 AGENTSPATH は実際のチェックアウトと異なり得る。スクリプト位置から解決する。
- ローカル `codex-cli 0.153.4` の `mcp add --help` で `NAME -- COMMAND...` を確認済み。永続的なツール承認設定を指定する専用オプションは表示されなかった。
- Codex 公式資料には `mcp_servers.<name>.default_tools_approval_mode`、`tools.<tool>.approval_mode`、enabled/disabled tools がある。列挙値だけから承認動作を推定せず、対象バージョンで確認する。
- GUI がシェルの PATH・環境変数を利用できるとは限らない。実行ファイルの絶対パスを使い、最小環境で起動確認する。
- MCP SDK のクラス名・clientInfo の取得・構造化エラーの API は固定するバージョンの実物で確認する。「公式最新」の API 名を未検証のまま固定しない。
- クライアントの MCP 起動位置・OS 権限・承認動作は個別に検証する。MCP の採用だけで全アプリのサンドボックス問題が解消するとは扱わない。

## レビュー対応表

| 指摘 | 設計 | 実装・検証 |
| --- | --- | --- |
| 1. ID のパス検証 | D3 | Task 1b、V3 |
| 2. 設定失敗時の誤保存 | D1・D2 | Task 1a、V2 |
| 3. 並行更新 | D6 | Task 1d、V4 |
| 4. Windows・GUI の導入 | D8 | Task 0b・3a、V7 |
| 5. 依存環境とテスト | D7 | Task 2a、V1 |
| 6. 抽出元・project の保持 | D4 | Task 1e・2b、V5 |
| 7. 設定更新時の保全 | D9 | Task 3a、V6 |
| 8. MCP 往復通信と TDD | D10 | Task 2b、V5 |

## 設計判断

### D1. 設定ファイルと失敗時の扱い

設定パスは次の順で決定する。

1. 明示された `LLM_MEMORY_CONFIG`
2. Windows: `%APPDATA%/llm-memory/config.toml`。APPDATA 未設定時は `~/AppData/Roaming/llm-memory/config.toml`
3. Unix: `$XDG_CONFIG_HOME/llm-memory/config.toml`。未設定時は `~/.config/llm-memory/config.toml`

設定は端末ローカルとし、同期しない。

```toml
vault = "~/Syncthing/llm-vault"
local_dir = "~/.agents/memory/local"
queue_dir = "~/.cache/llm-memory/queue"
```

- 文字列型・空文字・キー名を検証し、`~` と当該 OS の環境変数表記を展開する。Windows の TOML 例は `/` または literal string を使い、バックスラッシュのエスケープ事故を避ける。
- 相対パスと未解決の環境変数は拒否する。cwd により保存先が変わる設定を認めない。
- 既定探索先にファイルがない場合だけ「設定なし」とする。明示 `LLM_MEMORY_CONFIG` の不存在はエラー。
- 存在する設定の構文エラー、読み取り失敗、型不正、ディレクトリとファイルの取り違えは `MemoryConfigError` とし、代替 Vault へフォールバックしない。設定全体を検証し、環境変数による上書きがあっても壊れた設定を黙認しない。
- `tomllib` がなく設定ファイルが存在する場合は、対応 Python/uv での再実行を案内して失敗する。設定を無視して継続しない。設定なしの従来 CLI 経路だけは互換性を保つ。
- 書き込み時の権限エラー・I/O エラーも別の保存先への切替理由にしない。エラーに実際の保存先と原因を含め、秘密情報は出さない。
- 設定をすべて検証してからストアを作る。設定エラーでディレクトリ・session・event・observation を作らない。

### D2. 保存先の解決

有効な設定について **明示引数 > 環境変数 > 設定ファイル > 既定値**。指定値が不正なら下位の値へ落とさずエラー。`resolve_vault_dir()` の `(Path, used_fallback)` は維持する。

CLI は設定なしの場合の従来フォールバックと警告を維持する。MCP は意図しない保存を避けるため Vault の明示指定（環境変数または設定ファイル）を必須にする。local/queue は既定値を許容する。パスと出所は一度解決してプロセス中は固定し、設定変更後は再起動する。

`QUEUE_DIR` の import 時評価を `resolve_queue_dir(explicit=None)` に置き換える。CLI と MCP は同じ解決関数を使う。同じ明示設定で同じ絶対パスへ解決されることを比較する。テストは実際のユーザー設定を参照しないよう設定探索先を隔離する。

### D3. ID と保存領域の境界

MCP 層だけでなく、CLI と共有するストア層で検証する。

- session ID は単一のファイル名要素。既存の ID 形式を調査して互換性を保ちつつ、空・`.`・`..`・`/`・`\`・NUL・絶対パス・Windows drive/UNC/ADS 表記を拒否する。
- memory ID は `global/<slug>`、`projects/<project>/<slug>` など既存の active scope 配下の相対 ID のみ。生成索引・archive・未知のルートを通常操作の対象にしない。
- 読み取り元・書き込み先・archive 先をそれぞれ正規化し、期待するルートの内側か検証する。session JSON の `id` が要求 ID と異なる場合も拒否する。
- memory/local 配下の操作対象にあるシンボリックリンクや Windows junction/reparse point を拒否する。Vault 自体を設定されたリンクから解決する場合は、起動時の実体ルートを基準にする。
- 不正入力は `MemoryUsageError`。移動・更新・監査ログを含む副作用の前に拒否する。
- ローカルユーザーによる実行中の悪意あるファイルシステム差し替えは保護対象外と明記する。文字列の検証だけで OS レベルの隔離を保証しない。

### D4. ツールとセッション・出所

サーバー名は `shared-memory`。成功時は既存 CLI の JSON 形状を維持し、失敗は MCP tool error とする。

| ツール | 引数概要 | 成功結果 |
| --- | --- | --- |
| `get_context` | `project_id=None` | `ok`, `context` |
| `search` | query、memory_type、scope、project_id、entity_id、limit=10 | `ok`, `memories`, `count` |
| `history` | query、project_id、entity_id、memory_type、role、kind、limit=10、include_memories/sessions/events=True | `ok`, `query`, `project_id`, `memories`, `sessions`, `events`, `counts` |
| `write_memory` | key、summary、memory_type、confidence=0.8、scope=global、project_id=None、entity_type=user、entity_id=default、**session_id=None** | `ok`, `observation_id`, `event_id` |
| `forget` | memory_id、reason | `ok`, `updated`, `memory_id` |
| `list_unextracted` | limit=10 | `ok`, `sessions`, `count` |
| `mark_extracted` | session_id | `ok`, `updated` |

memory_type は profile/feedback/reference、検索 scope は global/project/client/temporary、書き込み scope は global/project。limit は 1〜100、confidence は有限な 0〜1 とし、入力型・必須値・ID は副作用前に検証する。追加制約で CLI の既存正常系が変わる場合は差分を明記する。

- 日常保存で session_id を省略した場合、入力検証後に遅延生成する。プロセス内で**実効 project_id ごと**にセッションを再利用し、異なる project を混ぜない。同時呼び出し時の生成も排他する。
- 自動セッションは `new_id('sess')`、user_id=default、実効 project_id、client=`mcp:<client-name>`。clientInfo が取得不能なら `mcp`。summary は作らない。
- project スコープの実効 project_id は既存の共通関数で決定する。global 記憶でも呼び出し元 project が指定されていればセッションの文脈として保持し、記憶の scope は変えない。
- 抽出処理は元の session_id を明示する。存在と ID を検証し、新しい自動セッションを作らず event/observation を元セッションへ結び付ける。
- 元セッションに project がある場合は文脈として継承する。指定 project と矛盾する場合、および元セッションの project がないのに別 project を付ける場合は拒否する。元セッションを書き換えて整合させない。
- `history(project_id=...)` で MCP 保存のイベントが取得でき、別 project のイベントが混ざらないことを確認する。記憶検索側の既存 scope 仕様は別途維持する。
- `memory-extract` は各保存結果を確認してから元セッションを mark_extracted する。途中失敗時は未処理のままにし、既存記憶を再検索して再開する。
- MCP の source/extractor 表記は Claude Code 専用の文字列を新規に流用せず、共通処理への明示オプションで渡す。既存 CLI の既定値は互換性を維持する。

### D5. ロジック分離とエラー

`run_*(args) -> dict` を抽出し、`cmd_*` は結果を `print_json` に渡す。MCP 層は `SimpleNamespace` または同等の引数オブジェクトを作る。CLI の既存オプション省略値（history の user_id/session_id 等）も漏れなく設定する。

必須は get_context/search/history/write_memory/forget/list_unextracted/mark_extracted/start_session/init_db の9処理。残りの全面的な機械分割は今回は行わず、共通のパス検証・ロック・設定を利用するために必要な箇所だけ変更する。

`MemoryUsageError` / `MemoryConfigError` を CLI で従来相当の非ゼロ終了と stderr に変換する。MCP の入力エラー・運用時 I/O エラーはツールエラーとし、サーバーを落とさない。起動時の設定不正は明確に終了する。想定外例外は stderr に診断を残し、キャンセル・終了シグナルを無差別に握り潰さない。

### D6. 並行更新と Syncthing

- 同一端末の CLI・各 MCP プロセスで共有する OS 対応のファイルロックを導入する。プロセス内の mutex だけにしない。利用ライブラリと Windows/Unix の動作は Phase 0 で確定し lock に固定する。
- ロック対象は正規化した store の実体パスで識別する。同期されない端末ローカルの固定ロック領域を利用し、local_dir の設定差で同じ Vault に別ロックが作られないようにする。
- 複数ロックが必要な操作は正規化キー順に取得する。タイムアウトを設け、明示的な再試行可能エラーを返す。タイムアウト時にロックなしで継続しない。
- Vault の既存記憶探索・slug 決定・read-modify-write・archive 先決定・索引再生成を一つの排他範囲にする。local の session 更新・JSONL 追記・重複判定も保護する。読み手が途中の JSONL を読まないよう読み取り側も共通ロックを利用する。
- 全 mutation 経路を監査する。CLI の consolidate/flush/cleanup/migrate と移行スクリプト、直接ストア呼び出しも対象。シェルラッパーの呼び出し形式は維持する。
- ロックはプロセス終了時に OS が解放する方式とし、ロックファイルの存在だけで所有権を判断しない。入れ子呼び出しの二重取得・デッドロックを避ける設計をテストする。
- 各ファイルの一意な一時名と atomic replace は維持する。event→observation→Vault 全体のクラッシュ時 atomicity は保証しない。部分失敗は成功扱いにせず、残存 observation と保存先を診断可能にし、既存 consolidate による回復手順を記載する。
- **端末間の Syncthing と人間の直接編集はローカルロックに参加しない。** 同じ記憶の端末間同時更新を避け、切り替え前に同期完了を確認する運用とする。競合検出だけで競合発生を防げるとは扱わない。
- 検出された同期競合ファイルは自動削除・自動統合しない。読み取りは警告し、Vault 更新は解消まで停止する。同期後の索引再生成手順を用意し、検索は現行同様 Markdown 実体を参照する。

### D7. パッケージと実行環境

`memory/pyproject.toml`（Python >=3.11、package=false）、`uv.lock`、`memory/.venv/` の ignore を追加する。MCP SDK、PyYAML、ロック用依存を Phase 0 の検証バージョンで固定する。更新は明示的な lock 更新で行う。

- MCP と全テストは `uv run --locked --project <memory-dir> ...` で実行する。導入時に `uv sync --locked --project <memory-dir>` を済ませ、初回ダウンロードと通常起動を分ける。
- `run-python.sh` は従来の選択優先順位を維持する。uv フォールバックだけロック済み project を使うが、「CLI も常に同じ環境」とは記載しない。
- 従来 Python での CLI 互換テストと、ロック済み環境での全テストを分ける。MCP 非導入 Python で MCP テストを discover するコマンドを標準手順にしない。
- 明示 Python/システム Python に tomllib がない場合は D1 を適用する。設定ありで誤フォールバックしないことを互換テストに含める。

### D8. OS・ツール別の導入

共通起動形式:

```text
<UV_ABS> run --locked --project <AGENTSPATH>/memory <AGENTSPATH>/memory/mcp_server.py
```

コマンドと引数は配列で生成し、空白・日本語を含むパスを shell 文字列へ連結しない。Unix は実行ファイルを解決し、Windows は PowerShell の Get-Command 等でネイティブ uv を解決する。WSL と Windows の実行ファイル・HOME・設定を混用しない。

| 環境 | Claude Code CLI | Codex CLI | Claude Desktop | Codex アプリ |
| --- | --- | --- | --- | --- |
| Ubuntu ネイティブ | user 登録を自動化、CLI 呼び出し確認 | user 登録を自動化、CLI 呼び出し確認 | 対象版の対応を Phase 0 で確認。非対応なら N/A と明記 | 対象版の対応を Phase 0 で確認。非対応なら N/A と明記 |
| Ubuntu / WSL | WSL 内 user 設定へ登録 | WSL 内 user 設定へ登録 | Windows 側の行で扱う | Windows 側の行で扱う |
| Windows ネイティブ | PowerShell 入口から登録。製品自身のランタイム要件は別途確認 | PowerShell 入口から登録 | Windows 設定用にローカル MCP エントリを生成・適用し GUI で検証 | Windows 側の設定参照先・起動位置を確認して登録、GUI で検証 |
| macOS | user 登録を自動化、CLI 呼び出し確認 | user 登録を自動化、CLI 呼び出し確認 | macOS 設定用にローカル MCP エントリを生成・適用し GUI で検証 | CLI と設定を共有する条件を確認し GUI で検証 |

- 共通の設定生成・差分適用処理と Unix/PowerShell の薄い起動入口を追加する。Windows の MCP 導入は make・Bash に依存させない。
- Claude Code は user スコープ、Claude Desktop は専用の `claude_desktop_config.json` の MCP エントリ、Codex は実際の CODEX_HOME の user 設定を対象とする。各設定の OS 別絶対位置は Task 0b で公式資料・実機から確定して README に記載する。
- CLI がない Desktop 単独環境にも共通生成器から登録できるようにする。公式 CLI を使えない登録先には D9 の限定更新を適用する。
- 各アプリの権限設定はアプリの対応範囲で適用する。Claude Code の allow を Desktop にそのまま適用できるとはしない。GUI で必要な認証・確認・再起動は手順書に残す。
- 実機がない組み合わせは生成テストのみ合格として記録し、実機動作済みと区別する。サポート状況の未確認を無言で N/A にしない。

### D9. クライアント設定の保全と権限

無条件の remove→add は採用しない。未登録時は公式 CLI が利用可能なら add、登録済みの場合はまず管理対象の非機密項目を比較する。変更不要なら書き込まない。

- 生成器が所有するのは対象サーバーの command/args と、ユーザーが選択したツール権限のみ。env・タイムアウト・他サーバー・未知キー・既存ユーザー設定を保持する。
- 既存ファイルの型・構造と更新予定を検証してから適用する。JSON/TOML の文字列置換や単純追記で重複セクションを作らない。TOML はコメント・未知テーブルを保持できる更新方式を Phase 0 で選定する。
- バックアップ・一時ファイルはユーザー設定と同等以下に制限された権限で、リポジトリや同期先へ置かない。既存リンクは切断せず、実体を明示して適用する。内容や認証値を出力しない。
- 書き込み直前に変更前の状態を再確認し、外部変更があれば中止する。atomic replace と適用後の構文・対象エントリ確認を行う。適用後検証に失敗した場合は、他者の変更を上書きしない条件で復旧する。
- CLI の設定再シリアライズでも未知キーや秘密情報が失われ得るため、公式 CLI という理由だけで安全と判断しない。認証値を含む設定全体を読む必要があり実行環境の規約で禁止される場合は自動適用せず、非機密の生成結果とアプリ自身の登録手順を提示する。
- 「未登録」と「権限・構文・実行失敗」を区別する。`2>/dev/null || true` で失敗を隠さない。missing executable は明示的な skipped、失敗は failed として返す。

権限の推奨は読み取り・write_memory・mark_extracted を自動実行、forget は人の確認とする。ただし既存の deny/ask や管理者ポリシーを弱めない。Codex の enum 値の意味は Task 0b の実測で確定し、未検証の `approve` を「人へ質問」の意味で焼き込まない。意図した動作を実現できないクライアントは制約を報告する。

### D10. TDD と MCP 統合検証

各 Task は失敗するテスト→最小実装→回帰確認の順。純粋な文書・設定例更新に実装追従だけのテストは追加しない。

MCP Python SDK の stdio クライアントで子プロセスを起動し、initialize 応答→initialized→tools/list→tools/call を順序どおりに実行する。SDK の初期化 API に手順を任せ、手書き JSON-RPC を一括送信して stdin を閉じる検証は使わない。

全試験で vault/local/queue/設定探索先を一時領域へ隔離する。タイムアウト・子プロセス回収を備え、実 Vault・実クライアント設定への書き込みをテストに使わない。

## 実装ステップ

### Phase 0: 実物 API と配布仕様の確定

#### Task 0a: SDK・依存・ロックの spike

仮 project で SDK バージョン、サーバークラス、stdio 起動、clientInfo、tool error、stdio ClientSession の往復を確認する。ロック依存のプロセス間排他・終了時解放と対応 OS を調査する。確認コード・出力・採用バージョンを計画へ追記する。uv の対象バージョンで --locked/--project が使えることも確認する。

#### Task 0b: クライアントと導入経路の確定

D8 の各組み合わせについて設定位置、起動環境、CLI 有無、ツール承認の設定と実際の動作、アプリ再起動の要否を記録する。Codex の登録構文は確認済みのため不足分だけ検証する。設定の限定更新方式・秘密情報を読まずに適用可能な範囲を確定する。

成果は実機確認/公式資料のみ/未確認/N/A を区別する対応表と登録例。以後の配布処理はこれに従う。

### Phase 1: 保存処理の前提整備

#### Task 1a: 設定解決のテストと実装

対象: 新規 memory_config.py / test_memory_config.py、既存 memory.py / markdown_store.py / local_store.py。

D1・D2 を実装。壊れた設定・明示設定の不存在・tomllib 不在・型不正・相対パス・読めない設定は誤保存しない。OS 分岐はパス計算の純粋関数を注入してテストし、os.name 差し替えだけで Path 実装を混乱させない。設定なしの CLI 互換と CLI/MCP の解決一致を確認する。

#### Task 1b: ID 境界のテストと実装

対象: 新規共通 path 検証モジュール、両ストアと関連テスト。

D3 の traversal/絶対パス/Windows 表記/リンク/内部 ID 不一致をテスト。対象外ファイルの内容と位置が変わらず、イベントも残らないことを確認する。正常な既存 ID の互換テストを残す。

#### Task 1c: run_* と例外の抽出

対象: memory.py、既存テストと新規直接呼び出しテスト。

D5 の9処理を分離。run_* が stdout を出さず dict を返すことを先にテストする。既存正常系の期待値を弱めず維持し、意図した不正入力の拒否だけ回帰条件を追加する。

#### Task 1d: プロセス間排他

対象: 新規 store ロックモジュール、両ストア、必要な CLI と移行処理、並行実行テスト。

D6 を実装。sleep に依存せず barrier 等で競合を発生させ、同じ記憶の更新・write/forget・別記憶と索引・local 更新を複数プロセスで検証する。raw CLI、MCP 相当、直接 store の経路でロックが共通になることを確認する。

#### Task 1e: 出所とセッション

対象: memory.py / local_store.py とテスト。

D4 の既存 session 利用、project 整合、source 表記を共通処理へ追加する。省略時の CLI 挙動を維持する。実効 project を欠いた不正入力は session/event/observation を残さない。

Phase 1 後、全保存先を隔離した既存 CLI デモと回帰試験を通す。

### Phase 2: MCP サーバー

#### Task 2a: ロック済み環境の整備

対象: memory/pyproject.toml / uv.lock / run-python.sh、.gitignore、検証用 Makefile ターゲット。

D7 に従い依存を固定。MCP 全テストと従来 Python の CLI 試験を分離し、README と Makefile が同じ標準コマンドを使う。

#### Task 2b: MCP テストを先に追加しサーバーを実装

対象: 新規 mcp_server.py / test_mcp_server.py / MCP 統合テスト。

D4 の7ツールのスキーマ、出力、入力拒否、セッション再利用を直接テストする。続いて D10 の子プロセス往復テストを追加して失敗を確認してから実装する。

完了条件: 7ツール公開、同 project の保存2回で自動 session は1件、異なる project は別 session、明示 session では自動生成なし。不正入力後に正常検索が成功し、stdout に JSON-RPC 以外が出ず、tool error が isError として認識される。

### Phase 3: 配布と権限

#### Task 3a: 保全する設定生成・適用と OS 入口

対象: scripts 配下の共通生成器・テスト、Unix/PowerShell 入口、deploy.sh、Makefile。

D8・D9 を実装。deploy.sh の既定 AGENTSPATH はスクリプト位置から決定する。uv の事前同期と起動確認後に登録する。既存の unrelated deploy 処理は変更しない。make memory-mcp-check は V5 のハンドシェイク試験を呼び出す。

一時設定へのテストで、再実行 no-op、既存 env/権限/未知キー/コメント保持、失敗時復旧、外部変更検知、空白・日本語パス、CLI 不在の Desktop 登録、ツール不在の skipped を検証する。模擬データだけを使用し実認証値を読まない。

#### Task 3b: クライアントでの権限検証

対象: .claude/settings.json と各クライアントの限定設定。

D9 の権限方針を確認済みの設定値へ変換する。既存の未コミット変更を保持する。実設定へ適用する際はセッションでの明示的な許可範囲とワークスペース外変更の規約を確認する。U1 未決でも生成・一時設定テストは完了させ、個人設定への権限変更だけを保留する。

### Phase 4: ドキュメント・スキル

#### Task 4a: スキル更新

対象: .claude/skills/{shared-memory,memory,memory-extract}/SKILL.md と sync による .codex 側。

日常操作を MCP へ移す。shared-memory は session_id 省略可、memory-extract は元 session_id 必須と使い分ける。設定エラーを CLI の別 Vault で回避しない。MCP 未導入環境用 CLI 手順は同じ明示設定を使う条件付きで残す。基盤運用 CLI、競合解消・部分失敗回復手順を memory に記載する。

#### Task 4b: README・設計書

対象: memory/README.md、README.md、memory/llm-shared-memory-design.md。

各 OS の uv 導入→依存同期→端末設定→登録→再起動→読み書き確認を掲載する。Windows 手順に make/Bash を要求しない。D8 の対応表には実機検証の有無を記載する。ローカル stdio の限界、端末間競合、CLI と MCP の Python 差、設定失敗時の停止を明記する。

## 判断事項

| # | 事項 | 扱い |
| --- | --- | --- |
| U1 | write 系ツールの権限 | forget のみ人が確認を推奨。ユーザー未決。生成・検証は進め、実権限変更前に決定 |
| U2 | uv.lock | コミットする計画とする。再現性のための通常の実装判断 |
| U3 | 設定パス | D1 の XDG/Windows 分岐を採用する計画 |
| U4 | 残り cmd_* 全面分割 | 見送り。安全性・共通化に必要な変更だけ実施 |
| U5 | サーバー名 | shared-memory を採用する計画 |
| U6 | repo .codex/config.toml | 端末固有コマンドは書かない。user 設定へ限定配布 |

## 検証手順

### V1. ロック済み全テストと CLI 互換

```bash
uv run --locked --project memory python -m unittest discover -s memory -p 'test_*.py'
```

従来 Python は既存 CLI テストを明示指定して別実行する（対象モジュール一覧は Task 2a で確定）。MCP SDK 不在の Python で MCP テストを含む discover を標準にしない。型検査・lint/format は既存プロジェクト設定を確認して変更範囲へ適用する。

### V2. 設定と保存先

設定なし、明示不存在、壊れた TOML、型不正、未展開変数、相対パス、tomllib 不在、権限エラーを検証。エラー時に代替 Vault と local/queue を含む副作用がないこと、CLI/MCP の正常な設定解決が一致することを確認する。

### V3. 保存領域外アクセス

CLI と MCP の双方で traversal、絶対パス、Windows drive/UNC/ADS、リンク、不正な session 内 id を拒否する。領域外の模擬ファイルの内容・位置が不変であること、エラー後も MCP が応答することを確認する。

### V4. 同時更新・障害

複数プロセスから同じ記憶を更新して各更新が現行値または履歴に残ること、索引が全 active 記憶と一致することを確認する。write/forget、JSONL 読み書き、ロック timeout、子プロセス強制終了後の再取得も確認する。同期競合ファイルを模擬し、警告・更新停止・原本保持を確認する。端末間の排他をテスト済みとは扱わない。

### V5. MCP の往復・出所

SDK stdio クライアントで initialize 応答を待ち、7ツールの schema を検証する。write→search→history、不正 write→search、抽出元 session への保存→mark_extracted を実行する。別 project 混入なし、同 project session 再利用、出所保持、stdout 純粋性、isError、タイムアウト時のプロセス回収を確認する。

### V6. 配布処理の保全

一時的な user 設定で追加・限定更新・no-op・外部変更・構文不正・適用失敗を試験する。既存設定と未知キーを保持し、復旧が他者の変更を上書きしないことを確認する。実クライアント設定は自動テストから触らない。

### V7. 実クライアント・各 OS

D8 の各対応環境で起動・検索・一時 Vault への保存・forget 承認を確認する。最小 PATH、空白/日本語パス、CLI のない Desktop、Windows と WSL の分離を含める。未検証環境は残課題として列挙し、生成テスト合格だけで完了にしない。

### V8. 後方互換・文書・独立レビュー

vault/local/queue/config を隔離して memory-init/memory-demo、Codex ラッパーの start-session→end-session 経路を検証する。技能同期後に check-skill-sync.sh を実行する。独立レビューでパス境界・全 mutation のロック利用・権限適用・例外・文書の残存する矛盾を確認する。

## 影響ファイルと実装順

- memory/: memory.py、両 store、run-python.sh、移行処理の必要箇所、既存テスト、README、設計書。
- 新規: memory_config.py、パス検証・ロック用モジュール、mcp_server.py、pyproject.toml/uv.lock、各ユニット・統合テスト。
- scripts/: 共通設定生成・限定適用、Unix/PowerShell の MCP 導入入口、関連テスト、deploy.sh。
- Makefile、.gitignore、必要な .claude/settings.json の限定変更、3スキルと同期先、ルート README。
- シェルラッパー・hook の MCP 化、ルールベース抽出ロジック、Markdown スキーマ、認証・マルチユーザー、HTTP transport は範囲外。安全性に必要な共通保存処理の修正は範囲内。

Phase 0 → Phase 1（各 Task 内で Red→Green）→ Phase 2（2a→2b のテスト→実装）→ Phase 3 → Phase 4 → V8。配布の仕様確認は Phase 0 で先に行う。文書のみの本レビュー対応では実装テストを走らせず、対応表・参照・旧矛盾の残存を検査する。

## 参考

- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/sandboxing
- https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
