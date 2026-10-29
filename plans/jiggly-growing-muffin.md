# Claude skills の skill 単位デプロイ（再計画: 単純 → 中程度）

## Context

git 管理外の skill をローカルで使えるようにするため、`scripts/deploy.sh` が `~/.claude/skills` をリポジトリへの
ディレクトリリンクにしていた構成を、「実ディレクトリ + skill 単位のリンク」（Codex と同じ方式）へ変える。
実行時データ（`_tracker/`・`synced/`）がリポジトリ内に溜まる問題も同時に解消する。

「単純」として RED → GREEN まで進めたが、検証（イテレーション 1）で不合格になった。

- **B1（退行）**: `hook-pre-boundary.sh` は `realpath ~/.claude/skills` を許可範囲にしている。新構成ではこれが実ディレクトリになる一方、
  `~/.claude/skills/<skill>/feedback/x.md` はリンクを辿ってリポジトリ内へ解決されるため、`~/.agents` 以外のプロジェクトから
  skill-feedback・skill-improve が書き込むと拒否される
- **B2 / B3（テスト不足）**: ドット始まりの古いリンクの片付けと、接頭辞の境界（`skills` そのもの・`skills-archive/...`）が未テスト

境界フックとそのテストが対象に加わり 1-2 ファイルに収まらないため、「中程度」に再分類して計画し直す。
あわせて、ユーザーの決定により insight-capture の新規 skill の作成先をリポジトリにする。

## 実装済み（維持する）

- `scripts/deploy.sh`: `is_skill_dir` / `ensure_real_dir` / `remove_stale_skill_links` / `deploy_skills`（Claude・Codex 共通）
- `.claude/scripts/hook-stop-insight-logger.sh`: debug-stop.log を `${HOME}/.claude/skills/_tracker` へ
- テスト: `scripts/test-deploy.sh`（243 件）、`scripts/test_hook_stop_insight_logger.py`（7 件）

## 追加の変更

### 1. 境界フック（B1）— `.claude/scripts/hook-pre-boundary.sh`（単純以上: tester → implementer）

許可範囲に「フックが属するリポジトリの `.claude/skills` の実体」を加える。
フックの実体パス（`realpath -m "${BASH_SOURCE[0]}"`）があるディレクトリ（`.claude/scripts`）から見た `../skills` を `realpath -m` で求め、`real_f` がその配下なら許可する。
既存の `real_skills`（`~/.claude/skills` の実体）の許可は残す（ローカル skill・`_tracker` 用）。

- 旧構成で実質的に許可されていた範囲（リポジトリの skills）と同じになり、許可範囲は広がらない
- フックは `settings.json` から `~/.agents/.claude/scripts/hook-pre-boundary.sh` として起動されるので、位置からリポジトリを特定できる
- 判定は従来どおり解決後のパス（`-s` なし）で行い、リンクで境界を抜けられないことを保つ
- ヘッダコメント 4 行目の例外の記述も新しい許可範囲に合わせる

テスト（`scripts/test_hook_pre_boundary.py`、既存の `HOOK_PRE_BOUNDARY_TARGET` で差し替え）:
- フックを一時ツリー `<tmp>/repo/.claude/scripts/` に複製し、`<tmp>/repo/.claude/skills/tdd/` を作る。stub HOME の `~/.claude/skills/tdd` → そのディレクトリへのリンクにする
- 他プロジェクトを cwd にして、`~/.claude/skills/tdd/feedback/x.md`・`~/.claude/skills/tdd/SKILL.md` が許可される
- `~/.claude/skills/_tracker/x.log`（実ディレクトリ側）が許可される
- 否定条件: リポジトリの `.claude/scripts/` や `.claude/settings.json`、`.claude/skills-archive/x`（接頭辞の兄弟）は拒否される
- 既存の `test_keeps_the_skills_and_memory_exceptions` などは維持する

### 2. deploy のテスト追加（B2 / B3 / 警告）— `scripts/test-deploy.sh`（tester のみ。実装は変更不要の見込み）

- B2: Claude 側・Codex 側それぞれで、`.x` 形式と `..x` 形式の古いリンクが取り除かれて報告されること、ドット始まりの別名リンクが残ること
- B3: `whole` → `$AGENTSPATH/.<tool>/skills`、`sibling` → `$AGENTSPATH/.<tool>/skills-archive/foo`、`sibling-broken` → 同 `missing` が残ること（両側）
- 警告: `~/.claude/skills` のリンクを外したとき `Unlinked:` が報告されることを契約にする
- 現在の実装で GREEN になるはずなので、RED 種別は invariant guard（変異 m01 / m10 / m07 で fail することを確認する）

### 3. insight-capture の作成先（ドキュメント）— オーケストレーターが直接編集

`.claude/skills/insight-capture/SKILL.md` の Step 3b-2 を、`~/.agents/.claude/skills/[スキル名]/SKILL.md` に作成し、
その後 `make -C ~/.agents deploy` で `~/.claude/skills` へリンクする手順に変える。`.codex/skills/insight-capture/SKILL.md` にも同じ変更を入れる
（現状も両者は同一内容）。Step 2 の一覧（`~/.claude/skills/` を読む）はローカル skill も含めて見るため変更しない。

### 4. 古いコメント — `scripts/test-check-skill-sync.sh:2085-2087`（tester）

「deploy.sh が ~/.claude/skills をリポジトリへ symlink するため配信物がリポジトリ内に現れる」という記述が現行構成と合わなくなる。
`synced/` の除外はリポジトリ内に残った旧データと未移行のマシンのために残すので、除外の理由を現行構成で説明する文に直す。

## 🛡️ 想定入力範囲

| 項目 | 内容 |
|---|---|
| 信頼境界 | Write/Edit の `file_path`（モデルが生成）は信頼しない。HOME 配下の既存状態はユーザー・Claude Code・Codex が置いた任意の形。AGENTSPATH 配下はリポジトリ管理物 |
| 想定する入力の軸 | deploy: 前回の計画どおり（`~/.<tool>/skills` 自体の状態、直下エントリ {正しい/壊れた/SKILL.md無し/下位/別名/接頭辞の外/接頭辞の兄弟/skills そのもの/ドット始まり（`.x`・`..x`）のリンク、実ディレクトリ、実ファイル}、リポジトリ側エントリ）。境界フック: 書き込み先 {skill リンク越しのリポジトリ内パス, `~/.claude/skills` 実ディレクトリ内, リポジトリの skills 以外の場所, 接頭辞の兄弟 `skills-archive`, `..` を含むパス, 存在しないパス} × cwd {リポジトリ内, 他プロジェクト} |
| 規模の上限 | skill 数は数十件。境界フックの既存タイムアウト設定に従う |
| 実行環境の前提 | bash, `set -e`（deploy）、PATH=/usr/bin:/bin、GNU coreutils（`realpath -m`）、`LC_ALL=C`（フック） |
| 想定しない条件 | 並行実行／AGENTSPATH の別表記で張られた旧リンク／`-` で始まる skill 名／ドット始まりのリポジトリ側 skill／`~/.codex/skills` 自体の既存状態／SKILL.md がディレクトリや壊れたリンク／フックがリポジトリ外へコピーされて起動される構成（settings.json は常にリポジトリ内を起動する） |
| 判定の不変条件 | deploy: deploy 後の `~/.<tool>/skills` 直下で、リポジトリの skills 配下を指すリンクはすべて skills 直下にあって SKILL.md を持つディレクトリを指し、各 skill には同名のリンクがあり、それ以外のエントリは実行前のまま残る。境界フック: 書き込み先の実体パスが、プロジェクト・`~/.claude/skills` の実体・フックが属するリポジトリの `.claude/skills` の実体・memory・自セッションのスクラッチパッドのいずれかの配下にあるときだけ許可する |

## 完了条件

- 上記 1・2・4 のテストがすべて GREEN。既存テスト（test-deploy.sh / test_hook_stop_insight_logger.py / test_hook_pre_boundary.py / test-check-skill-sync.sh ほか）が退行しない
  - `test_hook_session_start_philosophy.py` の時間計測 2 件は、今回の変更と無関係な flaky（フック・テストとも HEAD と同一で、skills を参照しない）として判定対象外
- 3 の変更後、`git add` した状態で `scripts/check-skill-sync.sh` が通る
- `@code-safety-inspector` の合格（イテレーションは 1 から数え直す）

除外範囲: `_tracker` / `synced` の除外ロジック（`.gitignore`・同期スクリプト・検査スクリプト）は削除しない。`~/.codex/skills` 自体の状態の扱いは変えない。

## 進め方

1. Phase 2a: `@tester` — 1 のテスト（behavior）、2 のテスト（invariant guard）、4 のコメント修正
2. Phase 2b: `@implementer` — `hook-pre-boundary.sh`
3. オーケストレーター — 3 を直接編集し、ミラー同期を確認
4. Phase 3: `@code-safety-inspector`（イテレーション 1）
5. 合格後、ユーザーにコミットを確認する

## 実 HOME への移行（コミット後、実行前に改めて確認を取る）

1回の Bash 呼び出しの中で次を行う。間で Stop フックが `_tracker` を作り直す前に移動を済ませるため。

1. `make -C ~/.agents deploy`（`~/.claude/skills` のリンクを外して実ディレクトリにし、skill をリンクする）
2. 移動先が存在しないことを確かめてから、`~/.agents/.claude/skills/_tracker` → `~/.claude/skills/_tracker` へ、`synced` も同様に `mv` する（削除しない）

その後に確認すること:
- `ls -la ~/.claude/skills` で各 skill がリンクになっていること
- **新しいセッションで skill が一覧に出ること**: 実ディレクトリ内に並べたシンボリックリンクを Claude Code が読み込むかは、まだ確かめていない
- 他プロジェクトから skill-feedback を書き込めること
- Codex 側で、リンク先を失っていた `code-investigator` / `code-planner` が片付いたこと

## 検証

- `bash scripts/test-deploy.sh` / `python3 scripts/test_hook_stop_insight_logger.py` / `python3 scripts/test_hook_pre_boundary.py` / `bash scripts/test-check-skill-sync.sh`
- `git add` 後に `bash scripts/check-skill-sync.sh`
- 移行後の確認は上記のとおり
