#!/usr/bin/env bash
# check-skill-sync.sh の振る舞いテスト
#
# 実行: scripts/test-check-skill-sync.sh
#
# 契約: 既定モードは検査のみで一切書き込まない。--auto-sync を渡したときだけ
# 同期スクリプトを起動し、生成物をステージする。
#
# 対象は破壊的（作業ツリーへの書き込みと git add）なので、3 重防御で隔離する。
#   1. 環境の遮断   : env -i で起動し、HOME / TMPDIR を $work 配下の stub に差し替える
#   2. 起動前ガード : 対象を起動するたび fatal_unless_isolated を通し、違反は exit 2 で即中断
#   3. 事後比較     : 全ケース終了後に実リポジトリのスナップショットが不変であることを検証する
#
# 対象は自身の位置を BASH_SOURCE ではなく git rev-parse --show-toplevel から得るため、
# cwd を一時リポジトリへ移すだけで、対象リポジトリが完全にそちらへ切り替わる。
#
# 同期スクリプトはスパイに差し替える。「作業ツリーが変化しない」だけでは
# 「起動されたが偶然変化しなかった」と区別できないため、起動回数を直接観測する。
# スパイは引数を記録したうえで本物の同期スクリプトへ exec する（pass-through spy）。
# 記録の書き込み先は一時リポジトリの外に置き、スパイ自身が作業ツリーを汚さないようにする。
#
# 対象パスは CHECK_SKILL_SYNC_TARGET で差し替えられる（充足可能性チェック・変異試験用）。

set -uo pipefail

# 出力の並び順とパターンマッチをロケールから独立させる。
export LC_ALL=C

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
target="${CHECK_SKILL_SYNC_TARGET:-$script_dir/check-skill-sync.sh}"
real_sync_src="$script_dir/sync-claude-codex-skills.sh"
bash_bin="$(command -v bash)"
real_repo="$(git -C "$script_dir" rev-parse --show-toplevel)"

work="$(cd "$(mktemp -d)" && pwd -P)"
trap 'chmod -R u+rwX "$work" 2>/dev/null; rm -rf "$work"' EXIT

pass_count=0
fail_count=0
skip_count=0
current_test=""

fail() {
    printf '  FAIL: %s\n' "$1"
    printf '%s' "${2:-}" | sed 's/^/        /'
    [ -n "${2:-}" ] && printf '\n'
    fail_count=$((fail_count + 1))
}

ok() {
    pass_count=$((pass_count + 1))
}

# 前提条件が満たせずケースを実行できなかったことを明示する。
# 黙って pass させると「実行された」と誤読されるため必ず出力する。
skip() {
    printf '  SKIP: %s\n' "$1"
    skip_count=$((skip_count + 1))
}

start_test() {
    current_test="$1"
    printf '\n== %s\n' "$current_test"
}

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        ok
    else
        fail "$label" "expected: [$expected]
actual  : [$actual]"
    fi
}

assert_match() {
    local label="$1" pattern="$2" actual="$3"
    if [[ "$actual" =~ $pattern ]]; then
        ok
    else
        fail "$label" "pattern: [$pattern]
actual : [$actual]"
    fi
}

assert_true() {
    local label="$1"
    if [ "$2" = "1" ]; then
        ok
    else
        fail "$label" "${3:-}"
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        ok
    else
        fail "$label" "needle: [$needle]
output:
$haystack"
    fi
}

assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        fail "$label" "needle: [$needle]
output:
$haystack"
    else
        ok
    fi
}

fatal() {
    printf 'FATAL: %s\n' "$1" >&2
    exit 2
}

# ---------------------------------------------------------------------------
# 隔離ガード
# ---------------------------------------------------------------------------

repo=""
home_stub="$work/home"
tmp_stub="$work/tmp"
real_sync="$work/real-sync.sh"

# 一時リポジトリの割り当て直後（git init 前）に通す軽量ガード。
fatal_unless_path_isolated() {
    local path="$1" what="$2"

    [ -n "$path" ] || fatal "$what のパスが空"

    case "$path" in
        "$work"/*) ;;
        *) fatal "$what が作業ディレクトリ外を指している: $path" ;;
    esac

    [ "$path" != "$real_repo" ] || fatal "$what が実リポジトリと一致している: $path"

    # 配下関係は両方向で確認する。片方向だけだと、$work を実リポジトリ配下に
    # 作った場合に素通りする。
    case "$path" in
        "$real_repo"/*) fatal "$what が実リポジトリ配下にある: $path" ;;
    esac
    case "$real_repo"/ in
        "$path"/*) fatal "実リポジトリが $what の配下にある: $path" ;;
    esac
}

# 対象を起動する直前に毎回通すガード。
fatal_unless_isolated() {
    local resolved toplevel

    fatal_unless_path_isolated "$repo" "一時リポジトリ"
    fatal_unless_path_isolated "$home_stub" "HOME stub"

    resolved="$(cd "$repo" 2>/dev/null && pwd -P)" \
        || fatal "一時リポジトリへ cd できない: $repo"
    toplevel="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null)" \
        || fatal "一時リポジトリが git リポジトリになっていない: $repo"

    # 対象は toplevel を起点に動くため、cwd と toplevel の一致が隔離の前提になる。
    [ "$resolved" = "$toplevel" ] || fatal "cwd と toplevel が不一致: [$resolved] [$toplevel]"

    [ -x "$repo/scripts/sync-claude-codex-skills.sh" ] \
        || fatal "同期スクリプトのスパイが配置されていない: $repo"
}

# 実リポジトリの状態。status --porcelain は追跡ファイルの変更と未追跡の追加を
# 両方拾う。既に変更済みのファイルが上書きされた場合を取り逃さないよう、
# 差分の内容もハッシュで固定する。
snapshot_real_repo() {
    git -C "$real_repo" status --porcelain
    printf -- '=== unstaged ===\n'
    git -C "$real_repo" diff | sha1sum
    printf -- '=== staged ===\n'
    git -C "$real_repo" diff --cached | sha1sum
}

real_repo_before="$(snapshot_real_repo)"

# ---------------------------------------------------------------------------
# 実行環境の構築
# ---------------------------------------------------------------------------

# 対象と同期スクリプトはファイルの作成・削除・コピーを行う。副作用の非発生を
# 検証するテストなので、書き込み系コマンドを PATH から外さない。外すと
# 「抑止できている」のか「そもそも失敗している」のかが区別できなくなる。
check_path="/usr/bin:/bin"
for cmd in git cp rm mkdir mktemp sed awk grep cat head basename dirname; do
    if ! env -i PATH="$check_path" "$bash_bin" -c "command -v $cmd" >/dev/null 2>&1; then
        fatal "required command not found in $check_path: $cmd"
    fi
done

[ -f "$target" ] || fatal "対象スクリプトが見つからない: $target"
[ -f "$real_sync_src" ] || fatal "同期スクリプトが見つからない: $real_sync_src"

mkdir -p "$home_stub" "$tmp_stub"
: > "$home_stub/.gitconfig"
mkdir -p "$work/nohooks"

cp "$real_sync_src" "$real_sync"
chmod +x "$real_sync"

# 一時リポジトリ向けの git。ユーザーの global / system 設定とフックを遮断する。
git_t() {
    env GIT_CONFIG_GLOBAL="$home_stub/.gitconfig" \
        GIT_CONFIG_NOSYSTEM=1 \
        HOME="$home_stub" \
        git -C "$repo" \
        -c user.name=tdd-tester \
        -c user.email=tdd@example.invalid \
        -c init.defaultBranch=main \
        -c core.hooksPath="$work/nohooks" \
        -c commit.gpgsign=false \
        "$@"
}

repo_seq=0
spy_log=""

write_agent() {
    cat > "$1" <<'AGENTEOF'
---
name: sample-agent
description: sample agent for tests
---

# Sample Agent

サンプルエージェントの本文。
AGENTEOF
}

write_skill() {
    local path="$1" body="$2"
    cat > "$path" <<SKILLEOF
---
name: alpha
description: alpha skill for tests
---

# Alpha Skill

$body
SKILLEOF
}

# new_repo: ケースごとに独立した一時リポジトリを構築する。
#
# .codex 側は同期スクリプトの出力と byte 単位で一致する形で置く。
# エージェントの変換はフロントマターを name / description だけで再生成するため、
# その 2 行だけを持つ入力なら往復して同一になる。これにより「初期状態は整合」を
# 実測なしに保証でき、無関係な差分がテストへ混入しない。
new_repo() {
    repo_seq=$((repo_seq + 1))
    repo="$work/repo$repo_seq"
    spy_log="$work/spy$repo_seq.log"
    extra_env=()

    fatal_unless_path_isolated "$repo" "一時リポジトリ"

    mkdir -p \
        "$repo/.claude/skills/alpha" \
        "$repo/.claude/agents" \
        "$repo/.codex/skills/alpha" \
        "$repo/.codex/skills/sample-agent" \
        "$repo/scripts"

    write_skill "$repo/.claude/skills/alpha/SKILL.md" "alpha の本文。"
    cp "$repo/.claude/skills/alpha/SKILL.md" "$repo/.codex/skills/alpha/SKILL.md"
    write_agent "$repo/.claude/agents/sample-agent.md"
    cp "$repo/.claude/agents/sample-agent.md" "$repo/.codex/skills/sample-agent/SKILL.md"
    printf 'stub readme\n' > "$repo/README.md"

    : > "$spy_log"
    cat > "$repo/scripts/sync-claude-codex-skills.sh" <<SPYEOF
#!/bin/sh
printf 'invoke:%s\n' "\$*" >> "$spy_log"
exec "$real_sync" "\$@"
SPYEOF
    chmod +x "$repo/scripts/sync-claude-codex-skills.sh"

    # 対象の 89 行目は HEAD が無いと非ゼロで返り 2>/dev/null される。
    # 初期コミットが無いと全ファイルが「差分あり」と誤判定される。
    git_t init -q || fatal "git init に失敗: $repo"
    git_t add -A || fatal "git add に失敗: $repo"
    git_t commit -q --no-verify -m "initial" || fatal "git commit に失敗: $repo"
}

# ---------------------------------------------------------------------------
# フィクスチャ（不整合・整合の作り分け）
# ---------------------------------------------------------------------------

# Claude 側だけをステージし、カウンターパートが存在しない状態を作る。
stage_claude_only_inconsistent() {
    mkdir -p "$repo/.claude/skills/newskill"
    write_skill "$repo/.claude/skills/newskill/SKILL.md" "新規スキルの本文。"
    git_t add .claude/skills/newskill/SKILL.md || fatal "add に失敗"
}

# Codex 側だけをステージし、カウンターパートの作業ツリーが HEAD からずれた状態を作る。
# 対象は git diff --quiet HEAD -- <counterpart> で作業ツリーを見るため、
# カウンターパートが HEAD と同一なら不整合として扱われない。
stage_codex_only_inconsistent() {
    write_skill "$repo/.codex/skills/alpha/SKILL.md" "codex 側で編集した本文。"
    git_t add .codex/skills/alpha/SKILL.md || fatal "add に失敗"
    write_skill "$repo/.claude/skills/alpha/SKILL.md" "claude 側の未ステージ編集。"
}

# 両側をステージしつつ、双方に未解決の不整合がある状態を作る。
stage_both_sides_inconsistent() {
    mkdir -p "$repo/.claude/skills/newskill" "$repo/.codex/skills/othernew"
    write_skill "$repo/.claude/skills/newskill/SKILL.md" "新規スキルの本文。"
    write_skill "$repo/.codex/skills/othernew/SKILL.md" "codex 側だけの新規スキル。"
    git_t add .claude/skills/newskill/SKILL.md .codex/skills/othernew/SKILL.md \
        || fatal "add に失敗"
}

# 両側を同じ内容でステージした整合状態。
stage_both_sides_consistent() {
    write_skill "$repo/.claude/skills/alpha/SKILL.md" "両側で揃えた本文。"
    cp "$repo/.claude/skills/alpha/SKILL.md" "$repo/.codex/skills/alpha/SKILL.md"
    git_t add .claude/skills/alpha/SKILL.md .codex/skills/alpha/SKILL.md \
        || fatal "add に失敗"
}

# ---------------------------------------------------------------------------
# スナップショットと実行ヘルパー
# ---------------------------------------------------------------------------

snapshot_worktree() {
    (
        cd "$repo" || return 1
        find . -path ./.git -prune -o -print | LC_ALL=C sort | while IFS= read -r f; do
            if [ -L "$f" ]; then
                printf 'l %s -> %s\n' "$f" "$(readlink "$f")"
            elif [ -d "$f" ]; then
                printf 'd %s\n' "$f"
            elif [ -f "$f" ]; then
                printf 'f %s %s\n' "$f" "$(sha1sum < "$f" | cut -d' ' -f1)"
            else
                printf '? %s\n' "$f"
            fi
        done
    )
}

snapshot_index() {
    git_t ls-files --stage
}

wt_before=""
ix_before=""

capture_before() {
    wt_before="$(snapshot_worktree)"
    ix_before="$(snapshot_index)"
    # 空のスナップショット同士は常に一致してしまう。比較が空振りするくらいなら
    # ここで止める（find や git が機能していないことを意味する）。
    [ -n "$wt_before" ] || fatal "作業ツリーのスナップショットが空"
    [ -n "$ix_before" ] || fatal "インデックスのスナップショットが空"
}

assert_worktree_unchanged() {
    assert_eq "$1: 作業ツリーが実行前後で一致" "$wt_before" "$(snapshot_worktree)"
}

assert_index_unchanged() {
    assert_eq "$1: インデックスが実行前後で一致" "$ix_before" "$(snapshot_index)"
}

spy_invocations() {
    local n=0 line
    if [ -f "$spy_log" ]; then
        while IFS= read -r line; do
            case "$line" in
                invoke:*) n=$((n + 1)) ;;
            esac
        done < "$spy_log"
    fi
    printf '%s\n' "$n"
}

assert_no_sync() {
    assert_eq "$1: 同期スクリプトが 1 度も起動されない" "0" "$(spy_invocations)"
}

out_file="$work/stdout.raw"
err_file="$work/stderr.raw"
run_out=""
run_err=""
run_all=""
run_rc=0
extra_env=()

run_check() {
    fatal_unless_isolated
    : > "$out_file"
    : > "$err_file"
    (
        cd "$repo" || exit 127
        env -i \
            PATH="$check_path" \
            HOME="$home_stub" \
            TMPDIR="$tmp_stub" \
            GIT_CONFIG_NOSYSTEM=1 \
            LC_ALL=C \
            ${extra_env[@]+"${extra_env[@]}"} \
            "$bash_bin" "$target" "$@"
    ) > "$out_file" 2> "$err_file"
    run_rc=$?
    run_out="$(cat "$out_file")"
    run_err="$(cat "$err_file")"
    run_all="$run_out
$run_err"
}

# 引数を受け付けない前提のケースをまとめて検証する。
# 不整合のあるリポジトリで実行するので、引数が無視されれば同期が走ってしまう。
assert_rejected_argument() {
    local label="$1"
    assert_eq "$label: exit 1" "1" "$run_rc"
    assert_true "$label: stderr にメッセージが出る" \
        "$([ -n "$run_err" ] && echo 1 || echo 0)" \
        "stdout: $run_out"
    assert_no_sync "$label"
    assert_worktree_unchanged "$label"
    assert_index_unchanged "$label"
}

# ---------------------------------------------------------------------------
# 前提の確認（フィクスチャが意図した不整合を作れているか）
# ---------------------------------------------------------------------------

start_test "前提: 隔離された一時リポジトリを構築できる"
new_repo
assert_eq "toplevel が一時リポジトリを指す" "$repo" "$(git_t rev-parse --show-toplevel)"
assert_true "実リポジトリとは別のパス" \
    "$([ "$repo" != "$real_repo" ] && echo 1 || echo 0)" "repo: $repo"
assert_eq "初期状態は clean" "" "$(git_t status --porcelain)"

start_test "前提: SKIP なしの既定実行が不整合を検出できる状態を作れる"
new_repo
stage_claude_only_inconsistent
assert_eq "Claude 側だけがステージされている" \
    ".claude/skills/newskill/SKILL.md" \
    "$(git_t diff --cached --name-only)"
assert_true "カウンターパートが存在しない" \
    "$([ ! -e "$repo/.codex/skills/newskill/SKILL.md" ] && echo 1 || echo 0)"

start_test "対象スクリプトが shellcheck を通る"
# 対象は現状 shellcheck クリーン。引数解析の追加でこれを崩さないことを固定する。
# 前提の判定は名前の存在確認だけで済ませず、既知のクリーンなスクリプトを
# 実際に検査させて期待どおり通ることまで確かめる。
shellcheck_usable=0
if command -v shellcheck >/dev/null 2>&1; then
    printf '#!/usr/bin/env bash\nset -euo pipefail\necho ok\n' > "$work/sc-probe.sh"
    if shellcheck "$work/sc-probe.sh" >/dev/null 2>&1; then
        shellcheck_usable=1
    fi
fi

if [ "$shellcheck_usable" = "1" ]; then
    sc_out="$(shellcheck "$target" 2>&1)"
    sc_rc=$?
    assert_eq "shellcheck が exit 0 で終わる" "0" "$sc_rc"
    assert_eq "shellcheck の指摘が無い" "" "$sc_out"
else
    skip "shellcheck が使える状態でないため、対象スクリプトの静的検査を実行できなかった"
fi

# ---------------------------------------------------------------------------
# 既定モード: 検査のみで一切書き込まない
# ---------------------------------------------------------------------------

start_test "既定モードは Claude 側の不整合を検出し、同期せずに exit 1 する"
new_repo
stage_claude_only_inconsistent
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "既定モード・Claude 側"
assert_worktree_unchanged "既定モード・Claude 側"
assert_index_unchanged "既定モード・Claude 側"
assert_true "カウンターパートが作られない" \
    "$([ ! -e "$repo/.codex/skills/newskill/SKILL.md" ] && echo 1 || echo 0)" \
    "tree: $(cd "$repo" && find .codex | LC_ALL=C sort | tr '\n' ' ')"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
assert_contains "不整合パスが報告される" ".claude/skills/newskill/SKILL.md" "$run_all"

start_test "既定モードの案内に --auto-sync と手動同期コマンドの両方が含まれる"
assert_contains "--auto-sync の案内がある" "--auto-sync" "$run_all"
assert_contains "手動同期コマンド（claude 起点）がある" \
    "scripts/sync-claude-codex-skills.sh --from claude" "$run_all"
assert_contains "手動同期コマンド（codex 起点）がある" \
    "scripts/sync-claude-codex-skills.sh --from codex" "$run_all"

start_test "既定モードの失敗理由が「自動同期したが解消せず」でも「両側ステージで曖昧」でもない"
# 既定モードでは同期を試みていないので、既存の 2 分岐の文面はどちらも事実に反する。
assert_not_contains "自動同期を実行した旨の説明が出ない" \
    "Automatic sync ran" "$run_all"
assert_not_contains "自動同期をスキップした旨の説明が出ない" \
    "Automatic sync is skipped" "$run_all"

start_test "既定モードは Codex 側の不整合でも同期せずに exit 1 する"
new_repo
stage_codex_only_inconsistent
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "既定モード・Codex 側"
assert_worktree_unchanged "既定モード・Codex 側"
assert_index_unchanged "既定モード・Codex 側"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
assert_not_contains "自動同期を実行した旨の説明が出ない" "Automatic sync ran" "$run_all"

start_test "既定モードは両側ステージの不整合でも第 3 の説明を出して exit 1 する"
new_repo
stage_both_sides_inconsistent
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "既定モード・両側ステージ"
assert_worktree_unchanged "既定モード・両側ステージ"
assert_index_unchanged "既定モード・両側ステージ"
assert_not_contains "曖昧さを理由にした説明が出ない" \
    "source of truth is ambiguous" "$run_all"
assert_not_contains "自動同期を実行した旨の説明が出ない" "Automatic sync ran" "$run_all"
assert_contains "--auto-sync の案内がある" "--auto-sync" "$run_all"

start_test "既定モードは整合していれば exit 0 で何も書き込まない"
new_repo
stage_both_sides_consistent
capture_before
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "既定モード・整合"
assert_worktree_unchanged "既定モード・整合"
assert_index_unchanged "既定モード・整合"

start_test "既定モードはステージが空なら exit 0 で何も書き込まない"
new_repo
capture_before
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "既定モード・ステージ空"
assert_worktree_unchanged "既定モード・ステージ空"
assert_index_unchanged "既定モード・ステージ空"

# ---------------------------------------------------------------------------
# --auto-sync: 同期と自動ステージ
# ---------------------------------------------------------------------------

start_test "--auto-sync は Claude 片側の不整合を同期して exit 0 する"
new_repo
stage_claude_only_inconsistent
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "同期スクリプトが 1 度だけ起動される" "1" "$(spy_invocations)"
assert_contains "--from claude で起動される" "invoke:--from claude" "$(cat "$spy_log")"
assert_true "カウンターパートが作られる" \
    "$([ -f "$repo/.codex/skills/newskill/SKILL.md" ] && echo 1 || echo 0)" \
    "tree: $(cd "$repo" && find .codex | LC_ALL=C sort | tr '\n' ' ')"
assert_contains "カウンターパートがステージされる" \
    ".codex/skills/newskill/SKILL.md" "$(git_t diff --cached --name-only)"

start_test "--auto-sync は Codex 片側の不整合を同期して exit 0 する"
new_repo
stage_codex_only_inconsistent
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "同期スクリプトが 1 度だけ起動される" "1" "$(spy_invocations)"
assert_contains "--from codex で起動される" "invoke:--from codex" "$(cat "$spy_log")"
assert_contains "カウンターパートがステージされる" \
    ".claude/skills/alpha/SKILL.md" "$(git_t diff --cached --name-only)"

start_test "--auto-sync でも両側ステージ時は同期せず exit 1 する"
new_repo
stage_both_sides_inconsistent
capture_before
run_check --auto-sync
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "--auto-sync・両側ステージ"
assert_worktree_unchanged "--auto-sync・両側ステージ"
assert_index_unchanged "--auto-sync・両側ステージ"
assert_contains "真実の源が曖昧である旨を説明する" \
    "source of truth is ambiguous" "$run_all"

start_test "--auto-sync は整合していれば同期を起動せず exit 0 する"
new_repo
stage_both_sides_consistent
capture_before
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "--auto-sync・整合"
assert_worktree_unchanged "--auto-sync・整合"
assert_index_unchanged "--auto-sync・整合"

start_test "--auto-sync の重複指定は単一指定と同じ結果になる"
new_repo
stage_claude_only_inconsistent
run_check --auto-sync --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "同期スクリプトが 1 度だけ起動される" "1" "$(spy_invocations)"
assert_true "カウンターパートが作られる" \
    "$([ -f "$repo/.codex/skills/newskill/SKILL.md" ] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# usage と不明な引数
# ---------------------------------------------------------------------------

start_test "-h は usage を stdout に出して exit 0 する"
new_repo
stage_claude_only_inconsistent
capture_before
run_check -h
assert_eq "exit 0" "0" "$run_rc"
assert_match "stdout に usage が出る" '[Uu]sage' "$run_out"
assert_contains "usage が --auto-sync に言及する" "--auto-sync" "$run_out"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "-h"
assert_worktree_unchanged "-h"
assert_index_unchanged "-h"

start_test "--help は usage を stdout に出して exit 0 する"
new_repo
stage_claude_only_inconsistent
capture_before
run_check --help
assert_eq "exit 0" "0" "$run_rc"
assert_match "stdout に usage が出る" '[Uu]sage' "$run_out"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "--help"
assert_worktree_unchanged "--help"
assert_index_unchanged "--help"

start_test "不明なロングオプションを拒否する"
new_repo
stage_claude_only_inconsistent
capture_before
run_check --typo
assert_rejected_argument "--typo"
assert_match "stderr に usage が出る" '[Uu]sage' "$run_err"

start_test "不明なショートオプションを拒否する"
new_repo
stage_claude_only_inconsistent
capture_before
run_check -x
assert_rejected_argument "-x"

start_test "空文字列の引数を拒否する"
new_repo
stage_claude_only_inconsistent
capture_before
run_check ""
assert_rejected_argument '空文字列'

start_test "ハイフン単体を拒否する"
new_repo
stage_claude_only_inconsistent
capture_before
run_check -
assert_rejected_argument "-"

start_test "ダブルハイフン単体を拒否する"
# 位置引数を取らないスクリプトなので、姉妹スクリプト
# sync-claude-codex-skills.sh の引数解析と同じく未知の引数として扱う。
new_repo
stage_claude_only_inconsistent
capture_before
run_check --
assert_rejected_argument "--"

start_test "不明な引数が --auto-sync より前にあっても同期しない"
new_repo
stage_claude_only_inconsistent
capture_before
run_check --typo --auto-sync
assert_rejected_argument "--typo --auto-sync"

start_test "不明な引数が --auto-sync より後にあっても同期しない"
# 引数を全部読み終える前に同期を始めると、この順序で書き込みが起きる。
new_repo
stage_claude_only_inconsistent
capture_before
run_check --auto-sync --typo
assert_rejected_argument "--auto-sync --typo"

# ---------------------------------------------------------------------------
# SKIP_SKILL_SYNC_CHECK による bypass
# ---------------------------------------------------------------------------

start_test "SKIP_SKILL_SYNC_CHECK=1 は引数なしで素通しする"
new_repo
stage_claude_only_inconsistent
capture_before
extra_env=("SKIP_SKILL_SYNC_CHECK=1")
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stdout は空" "" "$run_out"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "SKIP=1・引数なし"
assert_worktree_unchanged "SKIP=1・引数なし"
assert_index_unchanged "SKIP=1・引数なし"

start_test "SKIP_SKILL_SYNC_CHECK=1 は --auto-sync 付きでも素通しする"
new_repo
stage_claude_only_inconsistent
capture_before
extra_env=("SKIP_SKILL_SYNC_CHECK=1")
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "SKIP=1・--auto-sync"
assert_worktree_unchanged "SKIP=1・--auto-sync"
assert_index_unchanged "SKIP=1・--auto-sync"

start_test "SKIP_SKILL_SYNC_CHECK=1 は不明な引数付きでも素通しする"
# bypass は「何があっても素通しする」という契約なので、引数解析より前に効く。
new_repo
stage_claude_only_inconsistent
capture_before
extra_env=("SKIP_SKILL_SYNC_CHECK=1")
run_check --typo
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "SKIP=1・--typo"
assert_worktree_unchanged "SKIP=1・--typo"
assert_index_unchanged "SKIP=1・--typo"

start_test "SKIP_SKILL_SYNC_CHECK=0 は素通ししない"
new_repo
stage_claude_only_inconsistent
capture_before
extra_env=("SKIP_SKILL_SYNC_CHECK=0")
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "SKIP=0"
assert_worktree_unchanged "SKIP=0"
assert_index_unchanged "SKIP=0"

start_test "SKIP_SKILL_SYNC_CHECK が空文字列でも素通ししない"
new_repo
stage_claude_only_inconsistent
capture_before
extra_env=("SKIP_SKILL_SYNC_CHECK=")
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "SKIP=空"
assert_worktree_unchanged "SKIP=空"
assert_index_unchanged "SKIP=空"

# ---------------------------------------------------------------------------
# 事後スナップショット比較（実リポジトリの不変性）
# ---------------------------------------------------------------------------

start_test "テスト実行を通じて実リポジトリが変化しない"
assert_eq "実リポジトリの状態が実行前後で一致" "$real_repo_before" "$(snapshot_real_repo)"

# ---------------------------------------------------------------------------
printf '\n---------------------------------------------\n'
printf 'passed: %d  failed: %d  skipped: %d\n' "$pass_count" "$fail_count" "$skip_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0
