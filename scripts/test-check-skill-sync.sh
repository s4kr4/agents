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
# 対象はエージェント定義の比較に同期スクリプトの変換関数を再利用するため、同期
# スクリプトを source することがある。スパイは source されたか実行されたかを
# BASH_SOURCE と $0 の比較で見分け、別々の接頭辞で記録する。
#   invoke: 実行された（= 同期が走った）。assert_no_sync が数えるのはこちらだけ。
#   source: 読み込まれた（= 変換関数を借りただけ）。
# この 2 つを混ぜると「同期していない」という契約の検証が成立しなくなる。
#
# 対象の起動は timeout で包む。引数解析ループから shift が落ちると無限ループになり、
# テストは失敗せずにハングする。退行が無限待ちではなく失敗として現れるようにする。
#
# 対象パスは CHECK_SKILL_SYNC_TARGET で、待ち時間は CHECK_SKILL_SYNC_TIMEOUT で
# 差し替えられる（充足可能性チェック・変異試験用）。

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

# env は隔離の要なので PATH 解決に頼らず絶対パスで押さえる。
env_bin="$(command -v env)"
[ -n "$env_bin" ] || fatal "env が見つからない"

# 対象の起動 1 回につき timeout が 1 プロセス増えるが、ハングを失敗として
# 可視化できる価値のほうが大きい。待ち時間は正常系が余裕をもって終わる長さにする。
run_timeout="${CHECK_SKILL_SYNC_TIMEOUT:-60}"
timeout_exit_code=124

# 暴走した対象が出力を溜め込んでディスクを埋めないよう、上限を掛ける。
# 正常系の出力は数 KB なので、上限に触れること自体が異常の兆候になる。
# bash の ulimit -f は 1 KiB ブロックなので 8192 で約 8 MiB。
run_file_limit_blocks=8192
file_limit_exit_code=$((128 + $(kill -l SIGXFSZ 2>/dev/null || echo 25)))
timeout_bin="$(command -v timeout || true)"
timeout_usable=0
if [ -n "$timeout_bin" ]; then
    # 名前の存在確認では足りない。実際に打ち切らせて 124 が返ることまで確かめる。
    "$timeout_bin" 1 sleep 5 >/dev/null 2>&1
    if [ "$?" = "$timeout_exit_code" ]; then
        timeout_usable=1
    fi
fi
if [ "$timeout_usable" != "1" ]; then
    printf 'NOTE: timeout が使えないため、対象のハングを検出できません\n' >&2
fi

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

# 第 3 引数でフロントマターの追加行（model / color / tools）を渡せる。追加行は
# name / description の後ろに置く。to_claude_agent は dst のフロントマターを
# 行順のまま温存して description 行だけを差し替えるので、この順序が
# 往復同一性の前提になる。
write_agent() {
    local path="$1" body="${2:-サンプルエージェントの本文。}" extras="${3:-}"
    {
        printf -- '---\n'
        printf 'name: sample-agent\n'
        printf 'description: sample agent for tests\n'
        if [ -n "$extras" ]; then
            printf '%s\n' "$extras"
        fi
        printf -- '---\n\n'
        printf '# Sample Agent\n\n'
        printf '%s\n' "$body"
    } > "$path"
}

# Codex 側エージェントスキルの正規形。to_codex_agent_skill はフロントマターを
# name / description だけで再生成するため、extras を持たない形が変換出力と
# byte 単位で一致する。Claude 側に extras がある場合は cp では作れない。
write_codex_agent_skill() {
    local path="$1" body="${2:-サンプルエージェントの本文。}"
    write_agent "$path" "$body"
}

# フィクスチャが初期状態そのものを作り替える場合に使う。現在の作業ツリーを
# 初期コミットへ畳み込み、ステージが空の状態からケース本体を始められるようにする。
amend_initial_commit() {
    git_t add -A || fatal "amend 前の add に失敗"
    git_t commit -q --no-verify --amend -m "initial" || fatal "初期コミットの amend に失敗"
    [ -z "$(git_t status --porcelain)" ] \
        || fatal "amend 後もステージ/作業ツリーが clean でない: $repo"
}

# インデックスに載っている mode（100644 / 100755 / 120000）。未登録なら空。
# git が mode や symlink を記録しない環境ではケースの前提が作れないため、
# 名前の存在確認ではなく実際に記録された値で判定する。
staged_mode_of() {
    local entry
    entry="$(git_t ls-files --stage -- "$1")"
    printf '%s\n' "${entry%% *}"
}

# ステージされた変更の種別（A / M / D / T ...）。未ステージなら空。
# 通常ファイルと symlink の入れ替えは M ではなく T として現れる。T を記録しない
# 環境ではケースの前提が作れないため、名前ではなくこの実値で判定する。
staged_change_type_of() {
    local line
    line="$(git_t diff --cached --name-status -- "$1")"
    printf '%s\n' "${line%%$'\t'*}"
}

# インデックスに載っている blob の oid。未登録なら空。
staged_oid_of() {
    local entry rest
    entry="$(git_t ls-files --stage -- "$1")"
    rest="${entry#* }"
    printf '%s\n' "${rest%% *}"
}

# 両側のインデックスエントリが同一 blob を指していることを前提として固定する。
# 未登録なら空文字列同士の比較で素通りしてしまうので、その場合は即座に止める。
assert_staged_blobs_identical() {
    local a="$1" b="$2" oid_a oid_b
    oid_a="$(staged_oid_of "$a")"
    oid_b="$(staged_oid_of "$b")"
    [ -n "$oid_a" ] || fatal "インデックスに $a が載っていない"
    [ -n "$oid_b" ] || fatal "インデックスに $b が載っていない"
    assert_eq "前提: 両側の blob が byte 単位で一致している" "$oid_a" "$oid_b"
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
#
# 第 1 引数でスパイの種別を選ぶ。
#   passthrough（既定）: 引数を記録して本物の同期スクリプトへ exec する。
#                        source された場合は記録して本物の定義を読み込むだけ
#   partial            : 引数を記録し、カウンターパートを片方だけ作る。
#                        「同期は走ったが不整合が残る」状況を決定的に再現する
new_repo() {
    local spy_mode="${1:-passthrough}"

    repo_seq=$((repo_seq + 1))
    repo="$work/repo$repo_seq"
    spy_log="$work/spy$repo_seq.log"
    extra_env=()
    expect_timeout=0

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
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md"
    printf 'stub readme\n' > "$repo/README.md"

    # 実リポジトリと同じく feedback/ を無視する。両側の feedback/ は同期対象外で
    # 内容が食い違うのが正常なので、カウンターパートの比較がそこへ広がると
    # 恒久的な誤検出になる。その前提を再現しておく。
    cat > "$repo/.gitignore" <<'GITIGNOREEOF'
.claude/skills/*/feedback/
.codex/skills/*/feedback/
GITIGNOREEOF

    : > "$spy_log"
    case "$spy_mode" in
        passthrough)
            cat > "$repo/scripts/sync-claude-codex-skills.sh" <<SPYEOF
#!/bin/bash
# 対象は変換関数を再利用するためにこのスクリプトを source することがある。
# source されたときに exec すると対象自身のプロセスが置き換わってしまうので、
# 読み込みを記録して本物の定義だけを取り込み、呼び出し元へ戻る。
if [ "\${BASH_SOURCE[0]}" != "\$0" ]; then
    printf 'source:%s\n' "\$*" >> "$spy_log"
    . "$real_sync"
    return 0
fi
printf 'invoke:%s\n' "\$*" >> "$spy_log"
exec "$real_sync" "\$@"
SPYEOF
            ;;
        partial)
            # 対象は同期スクリプトを起動する前に repo root へ cd するので、
            # ここでは相対パスで作業してよい。
            # 受け取った --from に応じて、その方向のカウンターパートを片方だけ作る。
            # 想定外の方向で呼ばれたら黙って成功させず、stderr に出して失敗する。
            cat > "$repo/scripts/sync-claude-codex-skills.sh" <<SPYEOF
#!/bin/bash
# source 時の扱いは passthrough と同じ。ここで case の *) へ落とすと
# 変換関数を借りただけの source が「想定外の方向」として失敗してしまう。
if [ "\${BASH_SOURCE[0]}" != "\$0" ]; then
    printf 'source:%s\n' "\$*" >> "$spy_log"
    . "$real_sync"
    return 0
fi
printf 'invoke:%s\n' "\$*" >> "$spy_log"
echo "partial sync done"
case "\$*" in
    *'--from claude'*)
        mkdir -p .codex/skills/newskill
        cp .claude/skills/newskill/SKILL.md .codex/skills/newskill/SKILL.md
        ;;
    *'--from codex'*)
        mkdir -p .claude/skills/codexnew
        cp .codex/skills/codexnew/SKILL.md .claude/skills/codexnew/SKILL.md
        ;;
    *)
        echo "partial spy: unexpected arguments: \$*" >&2
        exit 1
        ;;
esac
SPYEOF
            ;;
        *)
            fatal "不明なスパイの種別: $spy_mode"
            ;;
    esac
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

# Claude 側だけをステージし、カウンターパートが 2 件とも存在しない状態を作る。
# partial スパイは newskill だけを同期するため、同期後も secondskill の不整合が残る。
stage_claude_only_two_inconsistencies() {
    mkdir -p "$repo/.claude/skills/newskill" "$repo/.claude/skills/secondskill"
    write_skill "$repo/.claude/skills/newskill/SKILL.md" "新規スキルの本文。"
    write_skill "$repo/.claude/skills/secondskill/SKILL.md" "2 つ目の新規スキルの本文。"
    git_t add .claude/skills/newskill/SKILL.md .claude/skills/secondskill/SKILL.md \
        || fatal "add に失敗"
}

# Codex 側だけを 2 つステージし、カウンターパートが 2 件とも存在しない状態を作る。
# partial スパイは codexnew だけを同期するため、同期後も codexsecond の不整合が残る。
stage_codex_only_two_inconsistencies() {
    mkdir -p "$repo/.codex/skills/codexnew" "$repo/.codex/skills/codexsecond"
    write_skill "$repo/.codex/skills/codexnew/SKILL.md" "codex 側の新規スキルの本文。"
    write_skill "$repo/.codex/skills/codexsecond/SKILL.md" "codex 側 2 つ目の本文。"
    git_t add .codex/skills/codexnew/SKILL.md .codex/skills/codexsecond/SKILL.md \
        || fatal "add に失敗"
}

# Codex 側だけをステージし、カウンターパートの作業ツリーが HEAD からずれた状態を作る。
# 対象は git diff --quiet HEAD -- <counterpart> で作業ツリーを見るため、
# カウンターパートが HEAD と同一なら不整合として扱われない。
stage_codex_only_inconsistent() {
    write_skill "$repo/.codex/skills/alpha/SKILL.md" "codex 側で編集した本文。"
    git_t add .codex/skills/alpha/SKILL.md || fatal "add に失敗"
    write_skill "$repo/.claude/skills/alpha/SKILL.md" "claude 側の未ステージ編集。"
}

# エージェントの Codex 側だけをステージし、カウンターパートの作業ツリーを
# HEAD からずらす。map_counterpart の .codex/skills/* → .claude/agents/*.md の
# 解決を通す唯一のフィクスチャ。
stage_agent_codex_side_inconsistent() {
    write_agent "$repo/.codex/skills/sample-agent/SKILL.md" "codex 側で編集した本文。"
    git_t add .codex/skills/sample-agent/SKILL.md || fatal "add に失敗"
    write_agent "$repo/.claude/agents/sample-agent.md" "claude 側の未ステージ編集。"
}

# エージェントの Claude 側だけをステージし、カウンターパートの作業ツリーを
# HEAD からずらす。map_counterpart の .claude/agents/*.md の分岐を通す。
stage_agent_claude_side_inconsistent() {
    write_agent "$repo/.claude/agents/sample-agent.md" "claude 側で編集した本文。"
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
    write_agent "$repo/.codex/skills/sample-agent/SKILL.md" "codex 側の未ステージ編集。"
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

# --- カウンターパートの内容が HEAD のまま取り残される状況 -------------------
#
# 以下のフィクスチャはいずれも「カウンターパートに未コミット変更が無い」状態を
# 作る。同期の判定をカウンターパートの未コミット変更の有無で行うと、どれも
# 整合と誤認されて片側だけのコミットが素通りする。

# Claude 側スキルを編集してステージし、Codex 側はワークツリーもインデックスも
# HEAD のままにする。
stage_claude_skill_stale_counterpart() {
    write_skill "$repo/.claude/skills/alpha/SKILL.md" "claude 側で更新した本文。"
    git_t add .claude/skills/alpha/SKILL.md || fatal "add に失敗"
}

# 上の鏡像。
stage_codex_skill_stale_counterpart() {
    write_skill "$repo/.codex/skills/alpha/SKILL.md" "codex 側で更新した本文。"
    git_t add .codex/skills/alpha/SKILL.md || fatal "add に失敗"
}

# エージェント定義の Claude 側だけを編集してステージし、Codex 側は無変更にする。
stage_agent_claude_stale_counterpart() {
    write_agent "$repo/.claude/agents/sample-agent.md" "claude 側で更新した本文。"
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
}

# 上の鏡像。
stage_agent_codex_stale_counterpart() {
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" "codex 側で更新した本文。"
    git_t add .codex/skills/sample-agent/SKILL.md || fatal "add に失敗"
}

# Claude 側エージェントの model 行だけを変更してステージする。description と
# 本文は変えない。Codex 側の正規形はフロントマターを name / description だけで
# 再生成するため、extras の変更はカウンターパートに影響しない。
stage_agent_claude_only_extras_changed() {
    local base_extras='model: sonnet
color: blue
tools: Read, Write'
    local changed_extras='model: opus
color: blue
tools: Read, Write'

    write_agent "$repo/.claude/agents/sample-agent.md" "エージェントの共通本文。" "$base_extras"
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" "エージェントの共通本文。"
    amend_initial_commit

    write_agent "$repo/.claude/agents/sample-agent.md" "エージェントの共通本文。" "$changed_extras"
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
}

# Claude 側が本文 B2 + extras、Codex 側が本文 B1 の初期状態を作り、Codex 側だけを
# B2 へ追いつかせてステージする。Claude 側はステージしない。
#
# to_claude_agent は非純粋で、既存の dst のフロントマターから model / color /
# tools を温存する。比較のために変換を走らせるとき dst 側へカウンターパートの
# 内容を事前コピーしないと extras が失われ、整合しているのに不整合と誤判定される。
# このフィクスチャがその事前コピーの唯一の検出器になる。
stage_codex_agent_catching_up_to_claude() {
    local extras='model: sonnet
color: blue
tools: Read, Write'

    write_agent "$repo/.claude/agents/sample-agent.md" "更新後の本文 B2。" "$extras"
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" "更新前の本文 B1。"
    amend_initial_commit

    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" "更新後の本文 B2。"
    git_t add .codex/skills/sample-agent/SKILL.md || fatal "add に失敗"
}

# Claude 側スキルの削除をステージし、カウンターパートはワークツリーからは
# 消えているがインデックスには残っている状態にする。
stage_claude_skill_deletion_unmirrored() {
    git_t rm -q .claude/skills/alpha/SKILL.md || fatal "git rm に失敗"

    # インデックスには残したいので git rm は使えない。ワークツリーからだけ
    # 作業ディレクトリの外へ退避する。
    mv "$repo/.codex/skills/alpha/SKILL.md" "$work/detached-codex-alpha$repo_seq.md" \
        || fatal "カウンターパートの退避に失敗"
}

# Claude 側だけをステージし、カウンターパートは同内容でワークツリーに存在するが
# git add されていない状態にする。内容が同じでもインデックスに載っていなければ
# コミットには入らない。
stage_claude_only_untracked_counterpart() {
    mkdir -p "$repo/.claude/skills/newskill" "$repo/.codex/skills/newskill"
    write_skill "$repo/.claude/skills/newskill/SKILL.md" "新規スキルの本文。"
    cp "$repo/.claude/skills/newskill/SKILL.md" "$repo/.codex/skills/newskill/SKILL.md"
    git_t add .claude/skills/newskill/SKILL.md || fatal "add に失敗"
}

# SKILL.md は両側で揃えてステージし、無視される feedback/ 配下だけ内容を
# 食い違わせる。比較がディレクトリ全体へ広がると誤検出になる。
stage_skill_consistent_with_feedback_divergence() {
    mkdir -p "$repo/.claude/skills/alpha/feedback" "$repo/.codex/skills/alpha/feedback"
    printf 'claude 側の未追跡メモ。\n' > "$repo/.claude/skills/alpha/feedback/notes.md"
    printf 'codex 側は別内容の未追跡メモ。\n' > "$repo/.codex/skills/alpha/feedback/notes.md"

    write_skill "$repo/.claude/skills/alpha/SKILL.md" "両側で揃えた本文。"
    cp "$repo/.claude/skills/alpha/SKILL.md" "$repo/.codex/skills/alpha/SKILL.md"
    git_t add .claude/skills/alpha/SKILL.md .codex/skills/alpha/SKILL.md \
        || fatal "add に失敗"
}

# 上のフィクスチャは両側がステージされるため、カウンターパートの内容比較へ
# 到達する前に「両側ステージ」で打ち切られる。比較を実際に走らせたうえで
# feedback/ の食い違いが持ち込まれないことを見るため、HEAD では Codex 側だけが
# 先に進んでいる状態を作り、Claude 側をそこへ追いつかせてステージする。
stage_claude_skill_catching_up_with_feedback_divergence() {
    write_skill "$repo/.codex/skills/alpha/SKILL.md" "追いついた先の本文 B2。"
    amend_initial_commit

    mkdir -p "$repo/.claude/skills/alpha/feedback" "$repo/.codex/skills/alpha/feedback"
    printf 'claude 側の未追跡メモ。\n' > "$repo/.claude/skills/alpha/feedback/notes.md"
    printf 'codex 側は別内容の未追跡メモ。\n' > "$repo/.codex/skills/alpha/feedback/notes.md"

    write_skill "$repo/.claude/skills/alpha/SKILL.md" "追いついた先の本文 B2。"
    git_t add .claude/skills/alpha/SKILL.md || fatal "add に失敗"
}

# 内容は同一で実行ビットだけ違う状態を作る。blob だけを比べると差が見えない。
stage_claude_skill_mode_diff() {
    chmod +x "$repo/.claude/skills/alpha/SKILL.md" || fatal "chmod に失敗"
    git_t add .claude/skills/alpha/SKILL.md || fatal "add に失敗"
}

# カウンターパートを symlink に置き換えた初期状態を作り、Claude 側を編集して
# ステージする。symlink 自身の内容（リンク先の文字列）は変わらないため、
# カウンターパートの未コミット変更を見る実装には差が見えない。
stage_claude_skill_symlink_counterpart() {
    (
        cd "$repo/.codex/skills/alpha" \
            && ln -sf ../../../.claude/skills/alpha/SKILL.md SKILL.md
    ) || fatal "symlink の作成に失敗"
    amend_initial_commit

    write_skill "$repo/.claude/skills/alpha/SKILL.md" "claude 側で更新した本文。"
    git_t add .claude/skills/alpha/SKILL.md || fatal "add に失敗"
}

# 両側を symlink にし、Claude 側のリンク先だけを Codex 側へ揃えてステージする。
# HEAD では Claude 側が別のファイルを指しているため、ステージされた変更は
# symlink 同士の内容変更として現れる（mode は 120000 のまま変わらないので
# 型変更にはならない）。
#
# ステージ後のインデックスは mode も blob（リンク先の文字列）も両側で一致する。
# 一致だけを見ると「同期済み」に見えるが、symlink は変換も内容比較も保証できない
# 型なので、一致していても判定不能として扱う必要がある。通常ファイル以外を
# 受理する実装を区別できるのはこのフィクスチャだけ。
stage_symlink_pair_identical_targets() {
    (
        cd "$repo/.claude/skills/alpha" \
            && ln -sf ../../../.gitignore SKILL.md
    ) || fatal "claude 側 symlink の作成に失敗"
    (
        cd "$repo/.codex/skills/alpha" \
            && ln -sf ../../../README.md SKILL.md
    ) || fatal "codex 側 symlink の作成に失敗"
    amend_initial_commit

    (
        cd "$repo/.claude/skills/alpha" \
            && ln -sf ../../../README.md SKILL.md
    ) || fatal "claude 側 symlink の差し替えに失敗"
    git_t add .claude/skills/alpha/SKILL.md || fatal "add に失敗"
}

# --- 型変更（通常ファイル → symlink）のミラー漏れ -------------------------
#
# 型変更はステージ一覧で M ではなく T として現れる。一覧の取得が T を拾わないと、
# 対象はこの変更を「ステージされていない」とみなし、内容比較を実装しても検査
# そのものを素通りする。
#
# 直前の symlink 系フィクスチャとは、ステージ一覧に現れるかどうかで役割が分かれる。
#   stage_symlink_pair_identical_targets : symlink → symlink（M。一覧に現れるので
#                                          mode / blob の比較が検出器になる）
#   ここから下                           : 通常ファイル → symlink（T。一覧に
#                                          現れるかどうかが検出器になる）

# Claude 側を通常ファイルから symlink へ差し替えてステージする。
# Codex 側は通常ファイルのまま無変更。
stage_claude_skill_type_change_unmirrored() {
    (
        cd "$repo/.claude/skills/alpha" \
            && ln -sf ../../../README.md SKILL.md
    ) || fatal "claude 側の型変更に失敗"
    git_t add .claude/skills/alpha/SKILL.md || fatal "add に失敗"
}

# 上の鏡像。
stage_codex_skill_type_change_unmirrored() {
    (
        cd "$repo/.codex/skills/alpha" \
            && ln -sf ../../../README.md SKILL.md
    ) || fatal "codex 側の型変更に失敗"
    git_t add .codex/skills/alpha/SKILL.md || fatal "add に失敗"
}

# 型変更が両側で正しくミラーされている状態。T を一覧へ含める変更が、正しく
# ミラーされた型変更まで不整合と誤判定しないことを固定する。
stage_type_change_mirrored_both_sides() {
    (
        cd "$repo/.claude/skills/alpha" \
            && ln -sf ../../../README.md SKILL.md
    ) || fatal "claude 側の型変更に失敗"
    (
        cd "$repo/.codex/skills/alpha" \
            && ln -sf ../../../README.md SKILL.md
    ) || fatal "codex 側の型変更に失敗"
    git_t add .claude/skills/alpha/SKILL.md .codex/skills/alpha/SKILL.md \
        || fatal "add に失敗"
}

# Claude 側エージェントの本文に NUL を混ぜてステージする。テキスト前提の変換を
# 通せないため、判定不能として扱われる（fail-closed）ことを固定する。
# NUL はソースへリテラルで埋め込めないので printf の 8 進表記で生成する。
stage_agent_claude_binary_body() {
    {
        printf -- '---\n'
        printf 'name: sample-agent\n'
        printf 'description: sample agent for tests\n'
        printf -- '---\n\n'
        printf '# Sample Agent\n\n'
        printf 'binary\000payload\n'
    } > "$repo/.claude/agents/sample-agent.md"
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
}

# --- 両側ステージでも内容の食い違いが残る状況 ------------------------------
#
# 「編集 → 同期 → もう一度編集 → git add -A」を通ると、対応ペアの両側がステージ
# された状態のまま内容だけが食い違う。カウンターパートがステージされているかどうか
# だけで打ち切ると、古いミラーがそのままコミットへ入る。

# 対応ペアのスキルを両側ステージし、内容だけを食い違わせる。
stage_both_sides_skill_divergent() {
    write_skill "$repo/.claude/skills/alpha/SKILL.md" "claude 側だけ先に進んだ本文。"
    write_skill "$repo/.codex/skills/alpha/SKILL.md" "codex 側に取り残された本文。"
    git_t add .claude/skills/alpha/SKILL.md .codex/skills/alpha/SKILL.md \
        || fatal "add に失敗"
}

# 上のエージェント版。写し先はスキルとは別の分岐で解決され、比較には変換が要る。
stage_both_sides_agent_divergent() {
    write_agent "$repo/.claude/agents/sample-agent.md" "claude 側だけ先に進んだ本文。"
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" \
        "codex 側に取り残された本文。"
    git_t add .claude/agents/sample-agent.md .codex/skills/sample-agent/SKILL.md \
        || fatal "add に失敗"
}

# 対応ペアのエージェント定義を両側ステージし、内容も揃えた整合状態。
# 両側ステージで内容比較を行う変更が、正しく揃ったペアを不整合と誤判定しない
# ことを固定する。変換を挟む分、スキルのペアより誤判定しやすい。
stage_both_sides_agent_consistent() {
    write_agent "$repo/.claude/agents/sample-agent.md" "両側で揃えた本文。"
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" "両側で揃えた本文。"
    git_t add .claude/agents/sample-agent.md .codex/skills/sample-agent/SKILL.md \
        || fatal "add に失敗"
}

# --- 恒等でない変換を挟むペアの byte 一致 ----------------------------------
#
# .claude/skills/* と .codex/skills/* は恒等コピーなので、インデックスの mode と
# blob が一致していれば同期済みと断定してよい。エージェントのペアは変換が恒等
# ではない（フロントマターの model / color / tools を落とす、~/.claude/ を
# リポジトリルートの絶対パスへ書き換える、@名前 をバッククォート形式へ書き換える）
# ため、byte 一致はむしろ変換を通していない証拠になる。
#
# 以下のフィクスチャはどれも「両側ステージ かつ mode と blob が完全一致」を作る。
# パス族を区別せずに一致だけで同期済みと確定させる実装を検出する。

# 変換で差が出る本文。Claude 側の表記形。
agent_body_claude_form='ルールは ~/.claude/rules/sample.md を参照する。委譲先は @code-planner。'

# 上の変換後の形。リポジトリルートを含むのでケースごとに組み立てる。
agent_body_codex_form() {
    # バッククォートはコマンド置換ではなく変換後の記法そのもの。
    # shellcheck disable=SC2016
    printf 'ルールは %s/.claude/rules/sample.md を参照する。委譲先は `code-planner`。' "$repo"
}

# 両側を Claude 側の表記形のまま byte 同一にして両側ステージする。
# フロントマターは name / description だけなので、差は本文の変換だけから生じる。
stage_agent_pair_identical_bytes_claude_form() {
    write_agent "$repo/.claude/agents/sample-agent.md" "$agent_body_claude_form"
    cp "$repo/.claude/agents/sample-agent.md" "$repo/.codex/skills/sample-agent/SKILL.md" \
        || fatal "コピーに失敗"
    git_t add .claude/agents/sample-agent.md .codex/skills/sample-agent/SKILL.md \
        || fatal "add に失敗"
}

# 上の鏡像。両側を Codex 側の表記形のまま byte 同一にして両側ステージする。
# Claude 側の正規形はバッククォート形式でも絶対パスでもないため、不整合になる。
stage_agent_pair_identical_bytes_codex_form() {
    local body
    body="$(agent_body_codex_form)"

    write_agent "$repo/.codex/skills/sample-agent/SKILL.md" "$body"
    cp "$repo/.codex/skills/sample-agent/SKILL.md" "$repo/.claude/agents/sample-agent.md" \
        || fatal "コピーに失敗"
    git_t add .claude/agents/sample-agent.md .codex/skills/sample-agent/SKILL.md \
        || fatal "add に失敗"
}

# 本文は変換の影響を受けない形にし、フロントマターに model 行だけを足して
# 両側 byte 同一にする。差は extras の有無だけから生じる。
stage_agent_pair_identical_bytes_with_extras() {
    write_agent "$repo/.claude/agents/sample-agent.md" "エージェントの共通本文。" 'model: opus'
    cp "$repo/.claude/agents/sample-agent.md" "$repo/.codex/skills/sample-agent/SKILL.md" \
        || fatal "コピーに失敗"
    git_t add .claude/agents/sample-agent.md .codex/skills/sample-agent/SKILL.md \
        || fatal "add に失敗"
}

# 変換で差が出る本文を持つペアを、正しく変換された形で両側ステージする。
# 上の 3 つと対になる整合側の固定。byte 一致を同期済みの根拠から外す変更が、
# 正しく変換されたペアまで不整合と誤判定しないことを見る。
stage_both_sides_agent_consistent_with_transform() {
    write_agent "$repo/.claude/agents/sample-agent.md" "$agent_body_claude_form"
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" \
        "$(agent_body_codex_form)"
    git_t add .claude/agents/sample-agent.md .codex/skills/sample-agent/SKILL.md \
        || fatal "add に失敗"
}

# --- 片側ステージのエージェントペア ----------------------------------------
#
# エージェントの変換は恒等ではないので、ペアの検証には「どちらを正とするか」の
# 向きがある。ステージされた側を正とすると、Codex 側だけがステージされたときに
# 「Codex→Claude 変換の結果が Claude 側と一致するか」を見ることになる。この向きは
# 情報を落とす変換の逆側なので、片側に反対側の表記の内容が紛れ込んでいても一致して
# しまう（to_claude_agent は dst の model / color / tools を温存し、本文の逆変換は
# 既に Claude 表記の文字列に対しては無変換になる）。
#
# 両側ステージのケース群と役割が分かれる。両側ステージではペアの両側が staged_set
# に入るため、どちらの向きの検証も一度は走る。片側ステージではステージされた側の
# 向きしか走らないので、向きの選び方がそのまま検出漏れになる。

agent_extras='model: sonnet
color: blue
tools: Read, Write'

# 変換で差が出る本文を持つ、正しく同期されたペアを初期コミットへ畳み込む。
# Claude 側は extras 付きの Claude 表記形、Codex 側はその変換後の形。
setup_agent_pair_in_sync_with_transform() {
    write_agent "$repo/.claude/agents/sample-agent.md" "$agent_body_claude_form" "$agent_extras"
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" \
        "$(agent_body_codex_form)"
    amend_initial_commit
}

# Codex 側へ Claude 表記の内容（extras 込み・本文は ~/.claude/ とバッククォート無し）
# をそのまま流し込み、Codex 側だけをステージする。Claude 側は無変更。
# 正規方向（--from claude）の同期を走らせれば Codex 側は書き換わるので、これは
# 実際にずれたミラーである。
stage_codex_agent_holding_claude_form() {
    setup_agent_pair_in_sync_with_transform

    cp "$repo/.claude/agents/sample-agent.md" "$repo/.codex/skills/sample-agent/SKILL.md" \
        || fatal "コピーに失敗"
    git_t add .codex/skills/sample-agent/SKILL.md || fatal "add に失敗"
}

# 上の鏡像。Claude 側へ Codex 表記の内容（extras 無し・本文は絶対パスと
# バッククォート形式）を流し込み、Claude 側だけをステージする。Codex 側は無変更。
# 正規方向（--from codex）の同期を走らせれば Claude 側は書き換わる。
stage_claude_agent_holding_codex_form() {
    setup_agent_pair_in_sync_with_transform

    cp "$repo/.codex/skills/sample-agent/SKILL.md" "$repo/.claude/agents/sample-agent.md" \
        || fatal "コピーに失敗"
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
}

# 正しく同期されたペアの Claude 側で extras だけを変え、Claude 側だけをステージする。
# Codex 側の正規形は extras を持たないので、ペアは整合したままになる。
stage_agent_claude_side_only_in_sync() {
    setup_agent_pair_in_sync_with_transform

    write_agent "$repo/.claude/agents/sample-agent.md" "$agent_body_claude_form" \
        'model: opus
color: blue
tools: Read, Write'
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
}

# 上の鏡像。Claude 側が先に B2 へ進んだ状態を初期コミットに置き、Codex 側だけを
# B2 の変換後の形へ追いつかせてステージする。Claude 側はステージしない。
stage_agent_codex_side_only_in_sync() {
    write_agent "$repo/.claude/agents/sample-agent.md" "$agent_body_claude_form" "$agent_extras"
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" "更新前の本文 B1。"
    amend_initial_commit

    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" \
        "$(agent_body_codex_form)"
    git_t add .codex/skills/sample-agent/SKILL.md || fatal "add に失敗"
}

# --- 判定不能な状態（fail-closed の検出器） --------------------------------

# 対応ペアの片側を stage 1/2/3 の競合エントリに置き換える。競合中のインデックス
# からは同期の可否を決められない。
stage_conflicted_index_entry() {
    local path=".claude/skills/alpha/SKILL.md"
    local base ours theirs

    base="$(printf 'base\n' | git_t hash-object -w --stdin)" || fatal "blob の作成に失敗"
    ours="$(printf 'ours\n' | git_t hash-object -w --stdin)" || fatal "blob の作成に失敗"
    theirs="$(printf 'theirs\n' | git_t hash-object -w --stdin)" || fatal "blob の作成に失敗"

    # stage 0 のエントリが残っていると競合エントリを載せられない。
    git_t rm -q --cached -- "$path" || fatal "インデックスからの除去に失敗"
    git_t update-index --index-info <<INDEXEOF || fatal "競合エントリの登録に失敗"
100644 $base 1	$path
100644 $ours 2	$path
100644 $theirs 3	$path
INDEXEOF
}

# エージェント派生スキルのディレクトリへ、対応関係を定義していない追加ファイルを
# 置いてステージする。写し先を決められないペア形状を同期済みとして素通しできない。
stage_codex_agent_extra_file() {
    printf '補助資料。\n' > "$repo/.codex/skills/sample-agent/reference.md"
    git_t add .codex/skills/sample-agent/reference.md || fatal "add に失敗"
}

# 本文に NUL を含むエージェント定義を、両側で正しく同期された状態に置く。
# Claude 側は extras だけを足してステージするので、テキストとして比較できさえ
# すれば整合と判定される。変換パイプラインはテキストを前提にしているため、
# 一致して見えても判定できたとは言えない。この形だけが、テキスト判定を外した
# 実装と原本を区別できる。NUL はソースへ直接埋め込めないので printf で生成する。
stage_agent_binary_pair_in_sync() {
    local synced="$work/binary-agent$repo_seq.md"

    {
        printf -- '---\n'
        printf 'name: sample-agent\n'
        printf 'description: sample agent for tests\n'
        printf -- '---\n\n'
        printf '# Sample Agent\n\n'
        printf 'binary\000payload\n'
    } > "$synced"

    cp "$synced" "$repo/.claude/agents/sample-agent.md" || fatal "コピーに失敗"
    cp "$synced" "$repo/.codex/skills/sample-agent/SKILL.md" || fatal "コピーに失敗"
    amend_initial_commit

    {
        printf -- '---\n'
        printf 'name: sample-agent\n'
        printf 'description: sample agent for tests\n'
        printf 'model: opus\n'
        printf -- '---\n\n'
        printf '# Sample Agent\n\n'
        printf 'binary\000payload\n'
    } > "$repo/.claude/agents/sample-agent.md"
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
}

# 変換関数の供給元を構文破損させる。関数定義そのものは読み込めるが source は
# 非ゼロで返る、という「一部だけ壊れた」状態を作る。読み込みの失敗を無視する
# 実装では変換が成功してしまうため、この形でなければ原本と区別できない。
corrupt_sync_script_after_definitions() {
    cat > "$repo/scripts/sync-claude-codex-skills.sh" <<SPYEOF
#!/bin/bash
if [ "\${BASH_SOURCE[0]}" != "\$0" ]; then
    printf 'source:%s\n' "\$*" >> "$spy_log"
    . "$real_sync"
fi
# 閉じていない if。ここまでの定義は読み込まれるが source は非ゼロで返る。
if [ "1" = "1"
SPYEOF
    chmod +x "$repo/scripts/sync-claude-codex-skills.sh"
}

# --- 変換の基準が環境から差し替えられる状況 --------------------------------

# エージェント本文の ~/.claude/ は、同期時にリポジトリルートの絶対パスへ
# 書き換えられる。Claude 側の extras だけを変えてステージするので、変換の基準が
# 正しい限り整合と判定される。基準が環境変数で乗っ取られると不整合に見える。
stage_agent_extras_changed_with_repo_root_path() {
    local body="ルールは ~/.claude/rules/sample.md を参照する。"

    write_agent "$repo/.claude/agents/sample-agent.md" "$body" 'model: sonnet'
    write_codex_agent_skill "$repo/.codex/skills/sample-agent/SKILL.md" \
        "ルールは $repo/.claude/rules/sample.md を参照する。"
    amend_initial_commit

    write_agent "$repo/.claude/agents/sample-agent.md" "$body" 'model: opus'
    git_t add .claude/agents/sample-agent.md || fatal "add に失敗"
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

# 任意のディレクトリツリーの内容。git 管理下にないデコイツリーの不変性を見る。
snapshot_tree() {
    (
        cd "$1" || return 1
        find . | LC_ALL=C sort | while IFS= read -r f; do
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

# 同期スクリプトが repo_root として受け取りうる、実リポジトリでも一時リポジトリ
# でもないツリー。同期が誤ってここを対象にすると extra.md が rm -rf で消える。
make_decoy_tree() {
    local dir="$1"

    fatal_unless_path_isolated "$dir" "デコイツリー"
    mkdir -p "$dir/.claude/skills/victim" "$dir/.codex/skills/victim" \
        || fatal "デコイツリーの作成に失敗"
    printf 'decoy claude skill\n' > "$dir/.claude/skills/victim/SKILL.md"
    printf 'decoy codex skill\n' > "$dir/.codex/skills/victim/SKILL.md"
    printf 'ディレクトリごと消されたことを検出するための目印。\n' \
        > "$dir/.codex/skills/victim/extra.md"
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

# 網羅性を測るときの母数の定義: 「実対象を起動し、かつこのガードを持つブロック」。
# 無条件に書き込む変異を当てて母数のブロックが残らず発火すれば、書き込みを検出すべき
# ブロックにガードの付け漏れが無いと言える。
#
# 数えるときは assert_rejected_argument のようなヘルパーの内側に隠れたガードを必ず
# 展開すること。展開を忘れると引数拒否系のブロックを数え落とし、母数と発火数が食い違う。
# 逆に「何らかの隔離ガードを持つ」という広い基準で数えると、起動回数しか見ないブロックが
# 紛れ込んで母数が膨らむ。--auto-sync で書き込みが正当に起きるケース群はこのガードを
# 持たないので、いずれの数え方でも母数には入らない。
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

# source による読み込み回数。「変換関数を借りた」ことを直接観測する。
# 起動（invoke）と読み込み（source）は別の事象なので、別々に数える。
spy_source_loads() {
    local n=0 line
    if [ -f "$spy_log" ]; then
        while IFS= read -r line; do
            case "$line" in
                source:*) n=$((n + 1)) ;;
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
run_timed_out=0
expect_timeout=0
extra_env=()

# 対象と同じ隔離環境（env -i / 制限 PATH / stub HOME・TMPDIR / timeout / ulimit）で
# 任意のスクリプトを起動する。同期スクリプト自身を検証するケースでも隔離を緩めない。
run_isolated() {
    local -a launcher=()

    fatal_unless_isolated
    : > "$out_file"
    : > "$err_file"
    run_timed_out=0

    if [ "$timeout_usable" = "1" ]; then
        launcher=("$timeout_bin" "$run_timeout")
    fi

    (
        cd "$repo" || exit 127
        # サイズ超過で対象が SIGXFSZ で落ちたときにコアダンプを残さない。
        ulimit -c 0 2>/dev/null || true
        ulimit -f "$run_file_limit_blocks" 2>/dev/null || true
        ${launcher[@]+"${launcher[@]}"} "$env_bin" -i \
            PATH="$check_path" \
            HOME="$home_stub" \
            TMPDIR="$tmp_stub" \
            GIT_CONFIG_NOSYSTEM=1 \
            LC_ALL=C \
            ${extra_env[@]+"${extra_env[@]}"} \
            "$bash_bin" "$@"
    ) > "$out_file" 2> "$err_file"
    run_rc=$?
    run_out="$(cat "$out_file")"
    run_err="$(cat "$err_file")"
    run_all="$run_out
$run_err"

    if [ "$timeout_usable" = "1" ] && [ "$run_rc" = "$timeout_exit_code" ]; then
        run_timed_out=1
        # ハングは「まだ終わっていない」ではなく失敗として扱う。ここで
        # 記録しないと、後続のアサーションの内容次第では素通りしうる。
        if [ "$expect_timeout" != "1" ]; then
            fail "対象が ${run_timeout} 秒以内に終了しなかった" "args: $*"
        fi
    fi

    if [ "$run_rc" = "$file_limit_exit_code" ]; then
        fail "対象の出力がサイズ上限（${run_file_limit_blocks} KiB）を超えた" "args: $*"
    fi
}

run_check() {
    run_isolated "$target" "$@"
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

start_test "プロダクションスクリプトが shellcheck を通る"
# 対象だけでなく同期スクリプトも検査する。同期スクリプトは対象から source される
# 側になるため、引数解析の移動やリファクタリングで静的な指摘を増やさないことを
# 固定する。前提の判定は名前の存在確認だけで済ませず、既知のクリーンな
# スクリプトを実際に検査させて期待どおり通ることまで確かめる。
shellcheck_usable=0
if command -v shellcheck >/dev/null 2>&1; then
    printf '#!/usr/bin/env bash\nset -euo pipefail\necho ok\n' > "$work/sc-probe.sh"
    if shellcheck "$work/sc-probe.sh" >/dev/null 2>&1; then
        shellcheck_usable=1
    fi
fi

if [ "$shellcheck_usable" = "1" ]; then
    for sc_target in "$target" "$real_sync_src"; do
        sc_name="$(basename "$sc_target")"
        sc_out="$(shellcheck "$sc_target" 2>&1)"
        sc_rc=$?
        assert_eq "shellcheck が exit 0 で終わる: $sc_name" "0" "$sc_rc"
        assert_eq "shellcheck の指摘が無い: $sc_name" "" "$sc_out"
    done
else
    skip "shellcheck が使える状態でないため、プロダクションスクリプトの静的検査を実行できなかった"
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

start_test "既定モードは Claude 側エージェント定義の不整合を Codex のスキルへ写して報告する"
# エージェント定義はスキルとは別の分岐で写される（拡張子を落として
# .codex/skills/<name>/SKILL.md へ移す）。この分岐が死んでも issue が 0 件に
# なるだけで他のケースは全て緑のままなので、専用のケースでしか守れない。
new_repo
stage_agent_claude_side_inconsistent
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "既定モード・エージェント Claude 側"
assert_worktree_unchanged "既定モード・エージェント Claude 側"
assert_index_unchanged "既定モード・エージェント Claude 側"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
assert_contains "不整合パスが報告される" ".claude/agents/sample-agent.md" "$run_all"
assert_contains "解消手順が Codex 側のスキルを指す" \
    "update and stage .codex/skills/sample-agent/SKILL.md" "$run_all"

start_test "既定モードは Codex 側エージェント定義の不整合を Claude の agents へ写して報告する"
# .codex/skills/<name>/ の写し先は既定では .claude/skills/<name>/ だが、同名の
# エージェント定義が実在する場合だけ .claude/agents/<name>.md になる。この代替枝が
# 落ちても exit 1 のままなので、終了コードでは検出できない。誤った写し先を
# 名指しで否定することだけが検出器になる。
new_repo
stage_agent_codex_side_inconsistent
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "既定モード・エージェント Codex 側"
assert_worktree_unchanged "既定モード・エージェント Codex 側"
assert_index_unchanged "既定モード・エージェント Codex 側"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
assert_contains "不整合パスが報告される" ".codex/skills/sample-agent/SKILL.md" "$run_all"
assert_contains "解消手順が既存のエージェント定義の更新を指す" \
    "update and stage .claude/agents/sample-agent.md" "$run_all"
assert_not_contains "存在しないスキル側のパスを案内しない" \
    ".claude/skills/sample-agent" "$run_all"
assert_not_contains "既存ファイルの更新なのに新規作成を指示しない" \
    "create and stage" "$run_all"

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

start_test "Claude→Codex の同期で解消しない場合、同期を実行したことと矛盾しない説明を出す"
# 同期スクリプトがカウンターパートを片方しか作らないと、対象は作れた側だけを
# git add する。その結果 .codex 側もステージ済みになり、再集計で「両側ステージ」
# と判定される。ステージしたのは利用者ではなく対象自身なので、
# 「曖昧だから同期をスキップした」という説明は事実に反する。
new_repo partial
stage_claude_only_two_inconsistencies
run_check --auto-sync
assert_eq "exit 1" "1" "$run_rc"
assert_eq "同期スクリプトが起動されている" "1" "$(spy_invocations)"
assert_contains "--from claude で起動される" "invoke:--from claude" "$(cat "$spy_log")"
assert_contains "同期が実際に走ったことが出力から読み取れる" "partial sync done" "$run_all"
assert_contains "解消しなかったパスが報告される" \
    ".claude/skills/secondskill/SKILL.md" "$run_all"
assert_not_contains "同期をスキップしたという説明が出ない" \
    "Automatic sync is skipped" "$run_all"
assert_not_contains "両側ステージを理由にした説明が出ない" \
    "Both Claude and Codex skill/agent files are staged in the same commit." "$run_all"
assert_contains "同期を実行したが解消しなかったことが読み取れる" \
    "Automatic sync ran" "$run_all"

start_test "Codex→Claude の同期で解消しない場合、同期を実行したことと矛盾しない説明を出す"
# 上の鏡像。同期を起動したという記録は方向ごとに別の代入で立つため、
# 片方だけを検証すると反対方向の退行を取り逃す。
new_repo partial
stage_codex_only_two_inconsistencies
run_check --auto-sync
assert_eq "exit 1" "1" "$run_rc"
assert_eq "同期スクリプトが起動されている" "1" "$(spy_invocations)"
assert_contains "--from codex で起動される" "invoke:--from codex" "$(cat "$spy_log")"
assert_contains "同期が実際に走ったことが出力から読み取れる" "partial sync done" "$run_all"
assert_contains "解消しなかったパスが報告される" \
    ".codex/skills/codexsecond/SKILL.md" "$run_all"
assert_not_contains "同期をスキップしたという説明が出ない" \
    "Automatic sync is skipped" "$run_all"
assert_not_contains "両側ステージを理由にした説明が出ない" \
    "Both Claude and Codex skill/agent files are staged in the same commit." "$run_all"
assert_contains "同期を実行したが解消しなかったことが読み取れる" \
    "Automatic sync ran" "$run_all"

start_test "--auto-sync で最初から両側ステージだった場合は従来どおり曖昧さを説明する"
# 上の分岐を足しても、利用者が本当に両側をステージした場合の説明は変えない。
new_repo
stage_both_sides_inconsistent
run_check --auto-sync
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "--auto-sync・利用者が両側ステージ"
assert_contains "両側ステージであることを説明する" \
    "Both Claude and Codex skill/agent files are staged in the same commit." "$run_all"
assert_contains "曖昧さを理由に同期をスキップしたと説明する" \
    "Automatic sync is skipped because the source of truth is ambiguous." "$run_all"
assert_not_contains "同期を実行したという説明は出ない" "Automatic sync ran" "$run_all"

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
# 既定モード: カウンターパートの内容で同期の有無を判定する
#
# ここから下のケースはいずれもカウンターパートに未コミット変更が無い。
# 未コミット変更の有無で判定すると全て「同期済み」と読めてしまい、片側だけが
# 更新されたコミットが通ってしまう。内容を比べてはじめて不整合として現れる。
# ---------------------------------------------------------------------------

start_test "既定モードは Claude 側スキルの更新に対しカウンターパートの内容が古いことを検出する"
new_repo
stage_claude_skill_stale_counterpart
capture_before
assert_eq "カウンターパートには未コミット変更が無い" "" \
    "$(git_t status --porcelain -- .codex/skills/alpha/SKILL.md)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "内容比較・Claude 側スキル"
assert_worktree_unchanged "内容比較・Claude 側スキル"
assert_index_unchanged "内容比較・Claude 側スキル"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
assert_contains "不整合パスが報告される" ".claude/skills/alpha/SKILL.md" "$run_all"
assert_contains "解消手順がカウンターパートの更新を指す" \
    "update and stage .codex/skills/alpha/SKILL.md" "$run_all"

start_test "--auto-sync は内容が古いカウンターパートを同期してステージする"
new_repo
stage_claude_skill_stale_counterpart
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "同期スクリプトが 1 度だけ起動される" "1" "$(spy_invocations)"
assert_contains "--from claude で起動される" "invoke:--from claude" "$(cat "$spy_log")"
assert_contains "カウンターパートがステージされる" \
    ".codex/skills/alpha/SKILL.md" "$(git_t diff --cached --name-only)"
assert_eq "ステージされた両側の内容が一致する" \
    "$(git_t cat-file -p :.claude/skills/alpha/SKILL.md)" \
    "$(git_t cat-file -p :.codex/skills/alpha/SKILL.md)"

start_test "既定モードは Codex 側スキルの更新に対しカウンターパートの内容が古いことを検出する"
new_repo
stage_codex_skill_stale_counterpart
capture_before
assert_eq "カウンターパートには未コミット変更が無い" "" \
    "$(git_t status --porcelain -- .claude/skills/alpha/SKILL.md)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "内容比較・Codex 側スキル"
assert_worktree_unchanged "内容比較・Codex 側スキル"
assert_index_unchanged "内容比較・Codex 側スキル"
assert_contains "不整合パスが報告される" ".codex/skills/alpha/SKILL.md" "$run_all"
assert_contains "解消手順がカウンターパートの更新を指す" \
    "update and stage .claude/skills/alpha/SKILL.md" "$run_all"

start_test "--auto-sync は Codex 起点でも内容が古いカウンターパートを同期する"
new_repo
stage_codex_skill_stale_counterpart
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "同期スクリプトが 1 度だけ起動される" "1" "$(spy_invocations)"
assert_contains "--from codex で起動される" "invoke:--from codex" "$(cat "$spy_log")"
assert_contains "カウンターパートがステージされる" \
    ".claude/skills/alpha/SKILL.md" "$(git_t diff --cached --name-only)"
assert_eq "ステージされた両側の内容が一致する" \
    "$(git_t cat-file -p :.codex/skills/alpha/SKILL.md)" \
    "$(git_t cat-file -p :.claude/skills/alpha/SKILL.md)"

start_test "既定モードは Claude 側エージェントの更新に対しカウンターパートの内容が古いことを検出する"
new_repo
stage_agent_claude_stale_counterpart
capture_before
assert_eq "カウンターパートには未コミット変更が無い" "" \
    "$(git_t status --porcelain -- .codex/skills/sample-agent/SKILL.md)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "内容比較・エージェント Claude 側"
assert_worktree_unchanged "内容比較・エージェント Claude 側"
assert_index_unchanged "内容比較・エージェント Claude 側"
assert_contains "不整合パスが報告される" ".claude/agents/sample-agent.md" "$run_all"
assert_contains "解消手順が Codex 側のスキルを指す" \
    "update and stage .codex/skills/sample-agent/SKILL.md" "$run_all"

start_test "既定モードはエージェント比較のため変換関数を読み込むが同期は実行しない"
# 直前のケースの実行結果を観測する。「読み込んだ」と「実行した」は別の事象なので、
# 作業ツリーの不変だけでは前者を確認できない。スパイの記録で直接見分ける。
assert_true "変換関数のために同期スクリプトが読み込まれる" \
    "$([ "$(spy_source_loads)" -ge 1 ] && echo 1 || echo 0)" \
    "spy log: $(cat "$spy_log")"
assert_eq "同期は 1 度も実行されない" "0" "$(spy_invocations)"

start_test "既定モードの出力に変換の進捗行が漏れない"
# 比較のための変換は利用者向けの出力ではない。sync agent: / sync dir: が出ると
# 「書き込みを伴う同期が走った」と誤読される。
assert_not_contains "エージェント変換の進捗行が出ない" "sync agent:" "$run_all"
assert_not_contains "ディレクトリ同期の進捗行が出ない" "sync dir:" "$run_all"

start_test "既定モードは Codex 側エージェントの更新に対しカウンターパートの内容が古いことを検出する"
new_repo
stage_agent_codex_stale_counterpart
capture_before
assert_eq "カウンターパートには未コミット変更が無い" "" \
    "$(git_t status --porcelain -- .claude/agents/sample-agent.md)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "内容比較・エージェント Codex 側"
assert_worktree_unchanged "内容比較・エージェント Codex 側"
assert_index_unchanged "内容比較・エージェント Codex 側"
assert_contains "不整合パスが報告される" ".codex/skills/sample-agent/SKILL.md" "$run_all"
assert_contains "解消手順が既存のエージェント定義の更新を指す" \
    "update and stage .claude/agents/sample-agent.md" "$run_all"

start_test "既定モードは Claude 側エージェントの model 行だけの変更を整合と判定する"
# Codex 側の正規形はフロントマターを name / description だけで再生成するため、
# model / color / tools の変更はカウンターパートへ影響しない。ステージされた
# パスの有無だけで判定すると、この変更が恒久的にコミット不能になる。
new_repo
stage_agent_claude_only_extras_changed
capture_before
assert_eq "ステージされているのは Claude 側だけ" \
    ".claude/agents/sample-agent.md" "$(git_t diff --cached --name-only)"
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "extras のみ変更"
assert_worktree_unchanged "extras のみ変更"
assert_index_unchanged "extras のみ変更"

start_test "既定モードは Codex 側の追いつき更新を整合と判定し extras を壊さない"
new_repo
stage_codex_agent_catching_up_to_claude
capture_before
assert_eq "ステージされているのは Codex 側だけ" \
    ".codex/skills/sample-agent/SKILL.md" "$(git_t diff --cached --name-only)"
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "Codex 側の追いつき更新"
assert_worktree_unchanged "Codex 側の追いつき更新"
assert_index_unchanged "Codex 側の追いつき更新"
# 比較のための変換先をリポジトリ内の実ファイルへ向けると、この 3 行が
# ワークツリーから消える。作業ツリー全体の一致でも検出できるが、壊れ方を
# 名指しで固定しておく。
agent_worktree="$(cat "$repo/.claude/agents/sample-agent.md")"
assert_contains "model 行がワークツリー上も残る" "model: sonnet" "$agent_worktree"
assert_contains "color 行がワークツリー上も残る" "color: blue" "$agent_worktree"
assert_contains "tools 行がワークツリー上も残る" "tools: Read, Write" "$agent_worktree"

start_test "既定モードは削除がミラーされていないことを検出し削除を促す"
new_repo
stage_claude_skill_deletion_unmirrored
capture_before
assert_eq "Claude 側の削除がステージされている" \
    ".claude/skills/alpha/SKILL.md" \
    "$(git_t diff --cached --name-only --diff-filter=D)"
assert_true "カウンターパートがインデックスに残っている" \
    "$([ -n "$(git_t ls-files -- .codex/skills/alpha/SKILL.md)" ] && echo 1 || echo 0)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "削除のミラー漏れ"
assert_worktree_unchanged "削除のミラー漏れ"
assert_index_unchanged "削除のミラー漏れ"
assert_contains "不整合パスが報告される" ".claude/skills/alpha/SKILL.md" "$run_all"
assert_contains "解消手順が削除とステージを指す" \
    "delete and stage .codex/skills/alpha/SKILL.md" "$run_all"
assert_not_contains "削除なのに新規作成を指示しない" "create and stage" "$run_all"

start_test "--auto-sync は削除のミラー漏れをステージして解消する"
new_repo
stage_claude_skill_deletion_unmirrored
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "同期スクリプトが 1 度だけ起動される" "1" "$(spy_invocations)"
assert_contains "カウンターパートの削除がステージされる" \
    ".codex/skills/alpha/SKILL.md" \
    "$(git_t diff --cached --name-only --diff-filter=D)"
assert_eq "カウンターパートがインデックスから消える" "" \
    "$(git_t ls-files -- .codex/skills/alpha/SKILL.md)"

start_test "既定モードは未追跡のカウンターパートをステージ漏れとして検出する"
new_repo
stage_claude_only_untracked_counterpart
capture_before
assert_eq "カウンターパートがインデックスに無い" "" \
    "$(git_t ls-files -- .codex/skills/newskill/SKILL.md)"
assert_true "カウンターパートはワークツリーには存在する" \
    "$([ -f "$repo/.codex/skills/newskill/SKILL.md" ] && echo 1 || echo 0)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "未追跡カウンターパート"
assert_worktree_unchanged "未追跡カウンターパート"
assert_index_unchanged "未追跡カウンターパート"
assert_contains "不整合パスが報告される" ".claude/skills/newskill/SKILL.md" "$run_all"
assert_contains "解消手順がステージを促す" \
    "stage .codex/skills/newskill/SKILL.md" "$run_all"

start_test "既定モードは無視される feedback/ の食い違いを不整合として扱わない"
new_repo
stage_skill_consistent_with_feedback_divergence
capture_before
assert_eq "feedback/ は git から無視されている" "" \
    "$(git_t status --porcelain -- .claude/skills/alpha/feedback .codex/skills/alpha/feedback)"
assert_true "両側の feedback/ の内容は食い違っている" \
    "$([ "$(cat "$repo/.claude/skills/alpha/feedback/notes.md")" \
        != "$(cat "$repo/.codex/skills/alpha/feedback/notes.md")" ] && echo 1 || echo 0)"
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "feedback の食い違い"
assert_worktree_unchanged "feedback の食い違い"
assert_index_unchanged "feedback の食い違い"

start_test "既定モードは内容比較を走らせても feedback/ の食い違いを持ち込まない"
# 片側だけがステージされているので、カウンターパートの内容比較が実際に走る。
# 比較が追跡対象のファイルではなくディレクトリ全体へ広がると、無視される
# feedback/ の差で恒久的な誤検出になる。
new_repo
stage_claude_skill_catching_up_with_feedback_divergence
capture_before
assert_eq "ステージされているのは Claude 側だけ" \
    ".claude/skills/alpha/SKILL.md" "$(git_t diff --cached --name-only)"
assert_true "カウンターパートはインデックス上で既に同じ内容" \
    "$([ "$(git_t rev-parse :.claude/skills/alpha/SKILL.md)" \
        = "$(git_t rev-parse :.codex/skills/alpha/SKILL.md)" ] && echo 1 || echo 0)"
assert_true "両側の feedback/ の内容は食い違っている" \
    "$([ "$(cat "$repo/.claude/skills/alpha/feedback/notes.md")" \
        != "$(cat "$repo/.codex/skills/alpha/feedback/notes.md")" ] && echo 1 || echo 0)"
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "内容比較 + feedback の食い違い"
assert_worktree_unchanged "内容比較 + feedback の食い違い"
assert_index_unchanged "内容比較 + feedback の食い違い"

start_test "既定モードは内容が同じで実行ビットだけ違う場合も不整合として検出する"
new_repo
stage_claude_skill_mode_diff
if [ "$(staged_mode_of .claude/skills/alpha/SKILL.md)" = "100755" ] \
    && [ "$(staged_mode_of .codex/skills/alpha/SKILL.md)" = "100644" ]; then
    capture_before
    assert_eq "blob は両側で同一" \
        "$(git_t rev-parse :.claude/skills/alpha/SKILL.md)" \
        "$(git_t rev-parse :.codex/skills/alpha/SKILL.md)"
    run_check
    assert_eq "exit 1" "1" "$run_rc"
    assert_no_sync "mode 差"
    assert_worktree_unchanged "mode 差"
    assert_index_unchanged "mode 差"
    assert_contains "不整合パスが報告される" ".claude/skills/alpha/SKILL.md" "$run_all"
else
    skip "git が実行ビットをインデックスへ記録しない環境のため、mode 差のケースを実行できなかった"
fi

start_test "既定モードは symlink のカウンターパートを判定不能として扱う"
new_repo
stage_claude_skill_symlink_counterpart
if [ "$(staged_mode_of .codex/skills/alpha/SKILL.md)" = "120000" ]; then
    capture_before
    assert_eq "symlink 自身には未コミット変更が無い" "" \
        "$(git_t status --porcelain -- .codex/skills/alpha/SKILL.md)"
    run_check
    assert_eq "exit 1" "1" "$run_rc"
    assert_no_sync "symlink カウンターパート"
    assert_worktree_unchanged "symlink カウンターパート"
    assert_index_unchanged "symlink カウンターパート"
    assert_contains "不整合パスが報告される" ".claude/skills/alpha/SKILL.md" "$run_all"
else
    skip "git が symlink をインデックスへ記録しない環境のため、symlink のケースを実行できなかった"
fi

start_test "既定モードは両側が同一リンク先の symlink でも判定不能として扱う"
new_repo
stage_symlink_pair_identical_targets
if [ "$(staged_mode_of .claude/skills/alpha/SKILL.md)" = "120000" ] \
    && [ "$(staged_mode_of .codex/skills/alpha/SKILL.md)" = "120000" ]; then
    capture_before
    # フィクスチャが「ステージされた変更」を作れていることを先に固定する。
    # 型変更（通常ファイル → symlink）は対象が見るステージ一覧に現れないため、
    # ここを確かめずに書くとケース全体が黙って空振りする。
    assert_eq "Claude 側の変更がステージされている" \
        ".claude/skills/alpha/SKILL.md" "$(git_t diff --cached --name-only)"
    assert_eq "インデックス上の blob が両側で一致している" \
        "$(git_t rev-parse :.claude/skills/alpha/SKILL.md)" \
        "$(git_t rev-parse :.codex/skills/alpha/SKILL.md)"
    run_check
    assert_eq "exit 1" "1" "$run_rc"
    assert_no_sync "同一リンク先の symlink"
    assert_worktree_unchanged "同一リンク先の symlink"
    assert_index_unchanged "同一リンク先の symlink"
    assert_contains "不整合パスが報告される" ".claude/skills/alpha/SKILL.md" "$run_all"
else
    skip "git が symlink をインデックスへ記録しない環境のため、symlink 一致のケースを実行できなかった"
fi

start_test "既定モードは Claude 側の型変更がミラーされていないことを検出する"
# 通常ファイル → symlink はステージ一覧で T として現れる。T を拾わないと対象は
# この変更を見落として exit 0 で素通りする。mode が 100644 から 120000 へ変わる
# ため、一覧に載れば既存の mode / blob 比較で不整合として判定できる。
new_repo
stage_claude_skill_type_change_unmirrored
if [ "$(staged_change_type_of .claude/skills/alpha/SKILL.md)" = "T" ]; then
    capture_before
    assert_eq "カウンターパートには未コミット変更が無い" "" \
        "$(git_t status --porcelain -- .codex/skills/alpha/SKILL.md)"
    assert_eq "カウンターパートは通常ファイルのまま" "100644" \
        "$(staged_mode_of .codex/skills/alpha/SKILL.md)"
    run_check
    assert_eq "exit 1" "1" "$run_rc"
    assert_no_sync "型変更・Claude 側"
    assert_worktree_unchanged "型変更・Claude 側"
    assert_index_unchanged "型変更・Claude 側"
    assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
    assert_contains "不整合パスが報告される" ".claude/skills/alpha/SKILL.md" "$run_all"
    assert_contains "解消手順がカウンターパートのステージを促す" \
        "stage .codex/skills/alpha/SKILL.md" "$run_all"
else
    skip "git が型変更をインデックスへ記録しない環境のため、Claude 側の型変更のケースを実行できなかった"
fi

start_test "既定モードは Codex 側の型変更がミラーされていないことを検出する"
# 上の鏡像。ステージ一覧の取得は方向を問わず 1 箇所なので、片方だけでも退行は
# 検出できるが、写し先の解決は方向ごとに別の分岐を通るため両方を固定する。
new_repo
stage_codex_skill_type_change_unmirrored
if [ "$(staged_change_type_of .codex/skills/alpha/SKILL.md)" = "T" ]; then
    capture_before
    assert_eq "カウンターパートには未コミット変更が無い" "" \
        "$(git_t status --porcelain -- .claude/skills/alpha/SKILL.md)"
    assert_eq "カウンターパートは通常ファイルのまま" "100644" \
        "$(staged_mode_of .claude/skills/alpha/SKILL.md)"
    run_check
    assert_eq "exit 1" "1" "$run_rc"
    assert_no_sync "型変更・Codex 側"
    assert_worktree_unchanged "型変更・Codex 側"
    assert_index_unchanged "型変更・Codex 側"
    assert_contains "不整合パスが報告される" ".codex/skills/alpha/SKILL.md" "$run_all"
    assert_contains "解消手順がカウンターパートのステージを促す" \
        "stage .claude/skills/alpha/SKILL.md" "$run_all"
else
    skip "git が型変更をインデックスへ記録しない環境のため、Codex 側の型変更のケースを実行できなかった"
fi

start_test "既定モードは両側でミラーされた型変更を整合と判定する"
new_repo
stage_type_change_mirrored_both_sides
if [ "$(staged_change_type_of .claude/skills/alpha/SKILL.md)" = "T" ] \
    && [ "$(staged_change_type_of .codex/skills/alpha/SKILL.md)" = "T" ]; then
    capture_before
    assert_eq "両側がステージされている" \
        ".claude/skills/alpha/SKILL.md
.codex/skills/alpha/SKILL.md" \
        "$(git_t diff --cached --name-only | LC_ALL=C sort)"
    run_check
    assert_eq "exit 0" "0" "$run_rc"
    assert_eq "stderr は空" "" "$run_err"
    assert_no_sync "型変更・両側ミラー"
    assert_worktree_unchanged "型変更・両側ミラー"
    assert_index_unchanged "型変更・両側ミラー"
else
    skip "git が型変更をインデックスへ記録しない環境のため、両側ミラーの型変更のケースを実行できなかった"
fi

start_test "既定モードはバイナリのエージェント定義を判定不能として扱う"
new_repo
stage_agent_claude_binary_body
capture_before
assert_match "git がバイナリとして扱っている" '^-[[:space:]]+-[[:space:]]' \
    "$(git_t diff --cached --numstat -- .claude/agents/sample-agent.md)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "バイナリのエージェント定義"
assert_worktree_unchanged "バイナリのエージェント定義"
assert_index_unchanged "バイナリのエージェント定義"
assert_contains "不整合パスが報告される" ".claude/agents/sample-agent.md" "$run_all"

# ---------------------------------------------------------------------------
# 同期スクリプト: source による変換関数の再利用と直接実行の後方互換
# ---------------------------------------------------------------------------

# 対象が変換関数を再利用できるかは、同期スクリプトを source しても同期が走らない
# ことにかかっている。プローブは一時リポジトリの外（$work 配下）に置き、対象と
# 同じ隔離環境で実行する。
source_probe="$work/source-probe.sh"
cat > "$source_probe" <<'PROBEEOF'
#!/usr/bin/env bash
set -uo pipefail

sync_script="$1"

# source される側へ位置パラメータを漏らさない。残すと同期スクリプトの引数解析が
# それを解釈してしまい、source 自体の可否を測れなくなる。
set --

# shellcheck source=/dev/null
. "$sync_script" || exit 4

for fn in to_claude_agent to_codex_agent_skill copy_dir; do
    if ! declare -F "$fn" >/dev/null 2>&1; then
        printf 'missing function: %s\n' "$fn" >&2
        exit 3
    fi
done

printf 'source probe ok\n'
PROBEEOF
chmod +x "$source_probe"

start_test "同期スクリプトを source しても同期が走らず変換関数だけが定義される"
new_repo
capture_before
run_isolated "$source_probe" "$real_sync"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_contains "プローブが最後まで到達する" "source probe ok" "$run_out"
assert_not_contains "ディレクトリ同期が走らない" "sync dir:" "$run_all"
assert_not_contains "エージェント変換が走らない" "sync agent:" "$run_all"
assert_worktree_unchanged "source プローブ"
assert_index_unchanged "source プローブ"

start_test "同期スクリプトの直接実行は --from claude --dry-run で計画だけを出力する"
new_repo
capture_before
run_isolated "$real_sync" --from claude --dry-run
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_contains "ディレクトリ同期の計画が出る" "sync dir: .claude/skills/alpha" "$run_out"
assert_contains "エージェント変換の計画が出る" \
    "sync agent: .claude/agents/sample-agent.md" "$run_out"
assert_worktree_unchanged "--from claude --dry-run"
assert_index_unchanged "--from claude --dry-run"

start_test "同期スクリプトの直接実行は --from の指定が無ければ exit 1 する"
new_repo
capture_before
run_isolated "$real_sync"
assert_eq "exit 1" "1" "$run_rc"
assert_contains "stderr で --from を要求する" "--from must be claude or codex" "$run_err"
assert_worktree_unchanged "--from 省略"
assert_index_unchanged "--from 省略"

start_test "同期スクリプトの直接実行は -h で usage を出して exit 0 する"
new_repo
capture_before
run_isolated "$real_sync" -h
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_match "stdout に usage が出る" '[Uu]sage' "$run_out"
assert_worktree_unchanged "同期スクリプトの -h"
assert_index_unchanged "同期スクリプトの -h"

# ---------------------------------------------------------------------------
# 既定モード: 両側ステージでも対応ペアの内容を比較する
# ---------------------------------------------------------------------------

start_test "既定モードは両側ステージでも対応ペアのスキルの食い違いを検出する"
new_repo
stage_both_sides_skill_divergent
capture_before
assert_eq "対応ペアの両側がステージされている" \
    ".claude/skills/alpha/SKILL.md
.codex/skills/alpha/SKILL.md" \
    "$(git_t diff --cached --name-only | LC_ALL=C sort)"
assert_true "ステージされた内容は両側で食い違っている" \
    "$([ "$(git_t rev-parse :.claude/skills/alpha/SKILL.md)" \
        != "$(git_t rev-parse :.codex/skills/alpha/SKILL.md)" ] && echo 1 || echo 0)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "両側ステージ・スキルの食い違い"
assert_worktree_unchanged "両側ステージ・スキルの食い違い"
assert_index_unchanged "両側ステージ・スキルの食い違い"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
# どちらを正とするかは利用者が決めるため、文面は固定せず両方のパスが示される
# ことだけを要求する。
assert_contains "Claude 側のパスが示される" ".claude/skills/alpha/SKILL.md" "$run_all"
assert_contains "Codex 側のパスが示される" ".codex/skills/alpha/SKILL.md" "$run_all"

start_test "既定モードは両側ステージでも対応ペアのエージェント定義の食い違いを検出する"
new_repo
stage_both_sides_agent_divergent
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "両側ステージ・エージェントの食い違い"
assert_worktree_unchanged "両側ステージ・エージェントの食い違い"
assert_index_unchanged "両側ステージ・エージェントの食い違い"
assert_contains "Claude 側のパスが示される" ".claude/agents/sample-agent.md" "$run_all"
assert_contains "Codex 側のパスが示される" ".codex/skills/sample-agent/SKILL.md" "$run_all"

start_test "既定モードは両側ステージのエージェント定義が揃っていれば exit 0 する"
new_repo
stage_both_sides_agent_consistent
capture_before
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "両側ステージ・エージェント整合"
assert_worktree_unchanged "両側ステージ・エージェント整合"
assert_index_unchanged "両側ステージ・エージェント整合"

# ---------------------------------------------------------------------------
# 恒等でない変換を挟むペアの byte 一致
#
# 同期済みかどうかを mode と blob の一致だけで確定させると、パス族の違いが
# 消える。スキル同士は恒等コピーなので一致が同期済みの根拠になるが、エージェント
# のペアは変換が恒等ではないので、一致は変換を通していない証拠になる。
# 単純コピーで作ったペアが exit 0 で受理されると、変換前の内容がそのまま
# コミットへ入る。
# ---------------------------------------------------------------------------

start_test "既定モードは Claude 側の表記のまま byte 同一なエージェントペアを整合と判定しない"
new_repo
stage_agent_pair_identical_bytes_claude_form
capture_before
assert_staged_blobs_identical .claude/agents/sample-agent.md \
    .codex/skills/sample-agent/SKILL.md
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "byte 同一・Claude 側の表記"
assert_worktree_unchanged "byte 同一・Claude 側の表記"
assert_index_unchanged "byte 同一・Claude 側の表記"
assert_contains "Claude 側のパスが示される" ".claude/agents/sample-agent.md" "$run_all"
assert_contains "Codex 側のパスが示される" ".codex/skills/sample-agent/SKILL.md" "$run_all"

start_test "既定モードは Codex 側の表記のまま byte 同一なエージェントペアを整合と判定しない"
new_repo
stage_agent_pair_identical_bytes_codex_form
capture_before
assert_staged_blobs_identical .claude/agents/sample-agent.md \
    .codex/skills/sample-agent/SKILL.md
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "byte 同一・Codex 側の表記"
assert_worktree_unchanged "byte 同一・Codex 側の表記"
assert_index_unchanged "byte 同一・Codex 側の表記"
assert_contains "Claude 側のパスが示される" ".claude/agents/sample-agent.md" "$run_all"
assert_contains "Codex 側のパスが示される" ".codex/skills/sample-agent/SKILL.md" "$run_all"

start_test "既定モードはフロントマターの extras ごと byte 同一なエージェントペアを整合と判定しない"
# 本文は変換の影響を受けないので、Codex 側が model 行を持っていること自体が
# 唯一の不整合になる。本文の変換とは別に固定する。
new_repo
stage_agent_pair_identical_bytes_with_extras
capture_before
assert_staged_blobs_identical .claude/agents/sample-agent.md \
    .codex/skills/sample-agent/SKILL.md
assert_contains "Codex 側に model 行が残っている" "model: opus" \
    "$(git_t cat-file blob ":.codex/skills/sample-agent/SKILL.md")"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "byte 同一・extras 付き"
assert_worktree_unchanged "byte 同一・extras 付き"
assert_index_unchanged "byte 同一・extras 付き"
assert_contains "Claude 側のパスが示される" ".claude/agents/sample-agent.md" "$run_all"
assert_contains "Codex 側のパスが示される" ".codex/skills/sample-agent/SKILL.md" "$run_all"

start_test "既定モードは変換済みのエージェントペアが両側ステージなら exit 0 する"
new_repo
stage_both_sides_agent_consistent_with_transform
capture_before
assert_true "前提: 両側の blob が一致していない" \
    "$([ "$(staged_oid_of .claude/agents/sample-agent.md)" \
        != "$(staged_oid_of .codex/skills/sample-agent/SKILL.md)" ] && echo 1 || echo 0)" \
    "oid: $(staged_oid_of .claude/agents/sample-agent.md)"
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "両側ステージ・変換済みエージェント"
assert_worktree_unchanged "両側ステージ・変換済みエージェント"
assert_index_unchanged "両側ステージ・変換済みエージェント"

# ---------------------------------------------------------------------------
# 片側ステージのエージェントペア
#
# 検証の向きがステージされた側に引きずられると、片側ステージのときだけ不正な
# ミラーが素通りする。向きはステージの状況ではなくパス族で決まるべきなので、
# 不整合側・整合側の両方を、両方向について固定する。
# ---------------------------------------------------------------------------

start_test "既定モードは Codex 側だけをステージした Claude 表記の混入を検出する"
new_repo
stage_codex_agent_holding_claude_form
capture_before
assert_eq "ステージされているのは Codex 側だけ" \
    ".codex/skills/sample-agent/SKILL.md" "$(git_t diff --cached --name-only)"
codex_staged="$(git_t cat-file blob :.codex/skills/sample-agent/SKILL.md)"
assert_contains "前提: Codex 側に extras が紛れ込んでいる" "model: sonnet" "$codex_staged"
# チルダは展開させたい対象ではなく、未変換であることの目印になる文字列そのもの。
# shellcheck disable=SC2088
assert_contains "前提: Codex 側の本文が未変換のまま" "~/.claude/rules/sample.md" "$codex_staged"
assert_contains "前提: Codex 側の委譲先表記が未変換のまま" "@code-planner" "$codex_staged"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "片側ステージ・Codex 側に Claude 表記"
assert_worktree_unchanged "片側ステージ・Codex 側に Claude 表記"
assert_index_unchanged "片側ステージ・Codex 側に Claude 表記"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
assert_contains "Codex 側のパスが示される" ".codex/skills/sample-agent/SKILL.md" "$run_all"
assert_contains "Claude 側のパスが示される" ".claude/agents/sample-agent.md" "$run_all"

start_test "既定モードは Claude 側だけをステージした Codex 表記の混入を検出する"
new_repo
stage_claude_agent_holding_codex_form
capture_before
assert_eq "ステージされているのは Claude 側だけ" \
    ".claude/agents/sample-agent.md" "$(git_t diff --cached --name-only)"
claude_staged="$(git_t cat-file blob :.claude/agents/sample-agent.md)"
assert_not_contains "前提: Claude 側から extras が失われている" "model: sonnet" "$claude_staged"
assert_contains "前提: Claude 側の本文が絶対パスのまま" \
    "$repo/.claude/rules/sample.md" "$claude_staged"
# バッククォートはコマンド置換ではなく Codex 側の記法そのもの。
# shellcheck disable=SC2016
assert_contains "前提: Claude 側の委譲先表記がバッククォート形式のまま" \
    '`code-planner`' "$claude_staged"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "片側ステージ・Claude 側に Codex 表記"
assert_worktree_unchanged "片側ステージ・Claude 側に Codex 表記"
assert_index_unchanged "片側ステージ・Claude 側に Codex 表記"
assert_contains "失敗ヘッダが stdout に出る" "Skill/agent sync check failed." "$run_out"
assert_contains "Claude 側のパスが示される" ".claude/agents/sample-agent.md" "$run_all"
assert_contains "Codex 側のパスが示される" ".codex/skills/sample-agent/SKILL.md" "$run_all"

start_test "既定モードは整合したペアの Claude 側だけがステージされていれば exit 0 する"
# 上の 2 ケースに合わせて検証を厳しくしても、正しく同期されたペアを不整合と
# 誤判定しないことを固定する。Claude 側の extras は Codex 側へ写らないので、
# extras だけの変更はペアを壊さない。
new_repo
stage_agent_claude_side_only_in_sync
capture_before
assert_eq "ステージされているのは Claude 側だけ" \
    ".claude/agents/sample-agent.md" "$(git_t diff --cached --name-only)"
assert_true "前提: 両側の blob は一致していない" \
    "$([ "$(staged_oid_of .claude/agents/sample-agent.md)" \
        != "$(staged_oid_of .codex/skills/sample-agent/SKILL.md)" ] && echo 1 || echo 0)" \
    "oid: $(staged_oid_of .claude/agents/sample-agent.md)"
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "片側ステージ・Claude 側が整合"
assert_worktree_unchanged "片側ステージ・Claude 側が整合"
assert_index_unchanged "片側ステージ・Claude 側が整合"

start_test "既定モードは整合したペアの Codex 側だけがステージされていれば exit 0 する"
# 上の鏡像。Claude 側はステージされていないので、検証は Claude 側のインデックスの
# 内容を使う必要がある。ステージされた側だけで判断すると、ここが判定不能になる。
new_repo
stage_agent_codex_side_only_in_sync
capture_before
assert_eq "ステージされているのは Codex 側だけ" \
    ".codex/skills/sample-agent/SKILL.md" "$(git_t diff --cached --name-only)"
assert_contains "前提: Claude 側のインデックスには extras が残っている" "model: sonnet" \
    "$(git_t cat-file blob :.claude/agents/sample-agent.md)"
run_check
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "片側ステージ・Codex 側が整合"
assert_worktree_unchanged "片側ステージ・Codex 側が整合"
assert_index_unchanged "片側ステージ・Codex 側が整合"
agent_worktree="$(cat "$repo/.claude/agents/sample-agent.md")"
assert_contains "model 行がワークツリー上も残る" "model: sonnet" "$agent_worktree"
assert_contains "color 行がワークツリー上も残る" "color: blue" "$agent_worktree"
assert_contains "tools 行がワークツリー上も残る" "tools: Read, Write" "$agent_worktree"

start_test "--auto-sync でも両側ステージの食い違いでは書き込まない"
new_repo
stage_both_sides_skill_divergent
capture_before
run_check --auto-sync
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "--auto-sync・両側ステージの食い違い"
assert_worktree_unchanged "--auto-sync・両側ステージの食い違い"
assert_index_unchanged "--auto-sync・両側ステージの食い違い"
assert_contains "Claude 側のパスが示される" ".claude/skills/alpha/SKILL.md" "$run_all"
assert_contains "Codex 側のパスが示される" ".codex/skills/alpha/SKILL.md" "$run_all"

# ---------------------------------------------------------------------------
# fail-closed: 判定できない状態は不整合として扱う
#
# いずれのケースも「同期できているかどうかを決められない」入力である。判定を
# 諦めて素通しすると、不正な状態が exit 0 で受理される。
# ---------------------------------------------------------------------------

# ls-files --stage だけを失敗させる git。判定に使う git が壊れたときに、対象が
# 「カウンターパートは無い」と読み替えないことを確かめる。
git_shim_dir="$work/git-shim"
git_bin="$(command -v git)"
[ -n "$git_bin" ] || fatal "git が見つからない"
mkdir -p "$git_shim_dir"
cat > "$git_shim_dir/git" <<GITSHIMEOF
#!/bin/bash
saw_ls_files=0
saw_stage=0
for arg in "\$@"; do
    case "\$arg" in
        ls-files) saw_ls_files=1 ;;
        --stage) saw_stage=1 ;;
    esac
done
if [ "\$saw_ls_files" = "1" ] && [ "\$saw_stage" = "1" ]; then
    exit 128
fi
exec "$git_bin" "\$@"
GITSHIMEOF
chmod +x "$git_shim_dir/git"

# shim が期待どおりに壊し、かつ他のサブコマンドを壊していないことを、名前では
# なく実際の挙動で確かめる。空振りしたまま pass させないための前提判定。
git_shim_probe="$work/git-shim-probe.sh"
cat > "$git_shim_probe" <<'GITPROBEEOF'
#!/usr/bin/env bash
set -uo pipefail

if git ls-files --stage -- . >/dev/null 2>&1; then
    printf 'shim が ls-files --stage を失敗させていない\n' >&2
    exit 1
fi
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    printf 'shim が無関係なサブコマンドまで壊している\n' >&2
    exit 1
fi
printf 'git shim probe ok\n'
GITPROBEEOF
chmod +x "$git_shim_probe"

start_test "既定モードは競合ステージを解消待ちとして扱い exit 1 する"
new_repo
stage_conflicted_index_entry
if [ -n "$(git_t ls-files --unmerged -- .claude/skills/alpha/SKILL.md)" ]; then
    capture_before
    run_check
    assert_eq "exit 1" "1" "$run_rc"
    assert_no_sync "競合ステージ"
    assert_worktree_unchanged "競合ステージ"
    assert_index_unchanged "競合ステージ"
    assert_contains "競合しているパスが報告される" \
        ".claude/skills/alpha/SKILL.md" "$run_all"
    assert_contains "解消手順が競合の解決とステージを促す" \
        "resolve and stage .claude/skills/alpha/SKILL.md" "$run_all"
else
    skip "git が競合エントリをインデックスへ記録しない環境のため、競合ステージのケースを実行できなかった"
fi

start_test "既定モードは git の失敗をカウンターパート不在と読み替えない"
new_repo
stage_claude_skill_deletion_unmirrored
extra_env=("PATH=$git_shim_dir:$check_path")
run_isolated "$git_shim_probe"
if [ "$run_rc" = "0" ]; then
    capture_before
    extra_env=("PATH=$git_shim_dir:$check_path")
    run_check
    assert_eq "exit 1" "1" "$run_rc"
    assert_no_sync "git の失敗"
    assert_worktree_unchanged "git の失敗"
    assert_index_unchanged "git の失敗"
    assert_contains "不整合パスが報告される" ".claude/skills/alpha/SKILL.md" "$run_all"
else
    skip "git の shim が期待どおり動かないため、git 失敗時のケースを実行できなかった"
fi

start_test "既定モードは対応関係を決められないペア形状を不整合として扱う"
new_repo
stage_codex_agent_extra_file
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "想定外のペア形状"
assert_worktree_unchanged "想定外のペア形状"
assert_index_unchanged "想定外のペア形状"
assert_contains "不整合パスが報告される" \
    ".codex/skills/sample-agent/reference.md" "$run_all"

start_test "既定モードは同期済みでもバイナリのエージェントペアを判定不能として扱う"
new_repo
stage_agent_binary_pair_in_sync
capture_before
assert_match "git がバイナリとして扱っている" '^-[[:space:]]+-[[:space:]]' \
    "$(git_t diff --cached --numstat -- .claude/agents/sample-agent.md)"
assert_eq "ステージされているのは Claude 側だけ" \
    ".claude/agents/sample-agent.md" "$(git_t diff --cached --name-only)"
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "同期済みのバイナリペア"
assert_worktree_unchanged "同期済みのバイナリペア"
assert_index_unchanged "同期済みのバイナリペア"
assert_contains "不整合パスが報告される" ".claude/agents/sample-agent.md" "$run_all"

start_test "既定モードは変換関数の供給元が壊れていれば整合と判定しない"
new_repo
stage_agent_extras_changed_with_repo_root_path
capture_before
run_check
assert_eq "前提: 供給元が健全なら整合と判定される" "0" "$run_rc"
corrupt_sync_script_after_definitions
capture_before
run_check
assert_eq "exit 1" "1" "$run_rc"
assert_no_sync "変換関数の供給元が壊れている"
assert_worktree_unchanged "変換関数の供給元が壊れている"
assert_index_unchanged "変換関数の供給元が壊れている"
assert_contains "不整合パスが報告される" ".claude/agents/sample-agent.md" "$run_all"

# ---------------------------------------------------------------------------
# 一時領域の後始末
# ---------------------------------------------------------------------------

start_test "既定モードを繰り返しても一時領域に残骸が蓄積しない"
# 比較用の一時ディレクトリにはリポジトリの内容が複製される。実行のたびに残ると
# 一時領域を圧迫し、複製された内容もそのまま残る。
new_repo
stage_claude_skill_stale_counterpart
tmp_probe="$work/tmp-probe$repo_seq"
mkdir -p "$tmp_probe"
capture_before
tmp_probe_unexpected=0
tmp_probe_i=0
while [ "$tmp_probe_i" -lt 5 ]; do
    tmp_probe_i=$((tmp_probe_i + 1))
    extra_env=("TMPDIR=$tmp_probe")
    run_check
    [ "$run_rc" = "1" ] || tmp_probe_unexpected=$((tmp_probe_unexpected + 1))
done
assert_eq "5 回とも不整合として exit 1 する" "0" "$tmp_probe_unexpected"
assert_eq "専用 TMPDIR に残骸が残らない" "" \
    "$(find "$tmp_probe" -mindepth 1 -maxdepth 1 | LC_ALL=C sort)"
assert_worktree_unchanged "一時領域の残骸"
assert_index_unchanged "一時領域の残骸"

# 専用 TMPDIR 配下の削除だけを失敗させる shim。後始末の失敗が終了コードへ
# 漏れないことを確かめるために使う。他のパスへの削除は本物へ委譲するので、
# 対象の通常動作は壊さない。
rm_shim_dir="$work/rm-shim"
rm_shim_log="$work/rm-shim.log"
mkdir -p "$rm_shim_dir"
for rm_shim_name in rm rmdir; do
    rm_shim_bin="$(command -v "$rm_shim_name" || true)"
    [ -n "$rm_shim_bin" ] || fatal "$rm_shim_name が見つからない"
    cat > "$rm_shim_dir/$rm_shim_name" <<RMSHIMEOF
#!/bin/bash
guard="\${RM_SHIM_GUARD:-}"
log="\${RM_SHIM_LOG:-/dev/null}"
hit=0
if [ -n "\$guard" ]; then
    for arg in "\$@"; do
        case "\$arg" in
            "\$guard"|"\$guard"/*) hit=1 ;;
        esac
    done
fi
if [ "\$hit" = "1" ]; then
    printf '%s:%s\n' "$rm_shim_name" "\$*" >> "\$log"
    exit 1
fi
exec "$rm_shim_bin" "\$@"
RMSHIMEOF
    chmod +x "$rm_shim_dir/$rm_shim_name"
done

start_test "一時領域の後始末が失敗しても終了コードを汚染しない"
new_repo
stage_claude_skill_catching_up_with_feedback_divergence
cleanup_tmp="$work/cleanup-tmp$repo_seq"
mkdir -p "$cleanup_tmp"
: > "$rm_shim_log"
capture_before
extra_env=(
    "PATH=$rm_shim_dir:$check_path"
    "TMPDIR=$cleanup_tmp"
    "RM_SHIM_GUARD=$cleanup_tmp"
    "RM_SHIM_LOG=$rm_shim_log"
)
run_check
if [ -s "$rm_shim_log" ]; then
    assert_eq "整合しているので exit 0 のまま" "0" "$run_rc"
    assert_no_sync "後始末の失敗"
    assert_worktree_unchanged "後始末の失敗"
    assert_index_unchanged "後始末の失敗"
else
    skip "一時領域の後始末に rm / rmdir を使わない実装のため、後始末失敗のケースを実行できなかった"
fi

# ---------------------------------------------------------------------------
# 環境変数の名前空間（repo_root / sync_repo_root）
#
# repo_root は汎用的な名前なので、利用者の環境に同名の変数がエクスポートされて
# いることがありうる。同期・検査の対象がそれで差し替わると、破壊的な処理
# （rm -rf を含むディレクトリ同期）が別のツリーへ向く。
#
# 現在の実装が実際に読む名前は、検査側が repo_root、同期側が sync_repo_root。
# デコイは両方の名前で渡す。実装が読まない名前だけを渡すと、ガードを外しても
# テストが落ちない（環境から継承する形へ戻す変異が素通りする）。旧名を残すのは、
# 同期側が repo_root を読む形へ戻る退行も同時に塞ぐため。
# ---------------------------------------------------------------------------

start_test "既定モードは環境の repo_root / sync_repo_root に変換の基準を乗っ取られない"
new_repo
stage_agent_extras_changed_with_repo_root_path
decoy="$work/decoy$repo_seq"
make_decoy_tree "$decoy"
decoy_before="$(snapshot_tree "$decoy")"
[ -n "$decoy_before" ] || fatal "デコイツリーのスナップショットが空"
capture_before
run_check
assert_eq "前提: 環境を汚さなければ整合と判定される" "0" "$run_rc"
extra_env=("repo_root=$decoy" "sync_repo_root=$decoy")
run_check
assert_eq "環境に repo_root / sync_repo_root があっても整合と判定される" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_no_sync "環境の repo_root / sync_repo_root・既定モード"
assert_worktree_unchanged "環境の repo_root / sync_repo_root・既定モード"
assert_index_unchanged "環境の repo_root / sync_repo_root・既定モード"
assert_eq "デコイツリーが変化しない" "$decoy_before" "$(snapshot_tree "$decoy")"

start_test "同期スクリプトの直接実行は環境の repo_root / sync_repo_root に同期先を乗っ取られない"
# copy_dir は同期先を rm -rf してから作り直す。同期先の決定が環境変数で
# 差し替えられると、その破壊的な処理が無関係なツリーへ向く。
new_repo
decoy="$work/decoy$repo_seq"
make_decoy_tree "$decoy"
decoy_before="$(snapshot_tree "$decoy")"
[ -n "$decoy_before" ] || fatal "デコイツリーのスナップショットが空"
mkdir -p "$repo/.claude/skills/newskill"
write_skill "$repo/.claude/skills/newskill/SKILL.md" "新規スキルの本文。"
extra_env=("repo_root=$decoy" "sync_repo_root=$decoy")
run_isolated "$real_sync" --from claude
assert_eq "exit 0" "0" "$run_rc"
assert_true "同期先は起動時のリポジトリになる" \
    "$([ -f "$repo/.codex/skills/newskill/SKILL.md" ] && echo 1 || echo 0)" \
    "tree: $(cd "$repo" && find .codex | LC_ALL=C sort | tr '\n' ' ')"
assert_eq "デコイツリーが変化しない" "$decoy_before" "$(snapshot_tree "$decoy")"

start_test "--auto-sync は環境の repo_root / sync_repo_root に同期先を乗っ取られない"
new_repo
stage_claude_only_inconsistent
decoy="$work/decoy$repo_seq"
make_decoy_tree "$decoy"
decoy_before="$(snapshot_tree "$decoy")"
[ -n "$decoy_before" ] || fatal "デコイツリーのスナップショットが空"
extra_env=("repo_root=$decoy" "sync_repo_root=$decoy")
run_check --auto-sync
assert_eq "exit 0" "0" "$run_rc"
assert_eq "同期スクリプトが 1 度だけ起動される" "1" "$(spy_invocations)"
assert_true "カウンターパートが一時リポジトリ側に作られる" \
    "$([ -f "$repo/.codex/skills/newskill/SKILL.md" ] && echo 1 || echo 0)" \
    "tree: $(cd "$repo" && find .codex | LC_ALL=C sort | tr '\n' ' ')"
assert_contains "カウンターパートがステージされる" \
    ".codex/skills/newskill/SKILL.md" "$(git_t diff --cached --name-only)"
assert_eq "デコイツリーが変化しない" "$decoy_before" "$(snapshot_tree "$decoy")"

# ---------------------------------------------------------------------------
# ハーネス自身の検査
# ---------------------------------------------------------------------------

start_test "対象がハングした場合にテストが失敗として現れる"
# 引数解析ループから shift が落ちると無限ループになる。タイムアウトが無いと
# テストは失敗せずに待ち続けるため、打ち切りが機能することを直接確かめる。
if [ "$timeout_usable" = "1" ]; then
    hang_stub="$work/hang-forever.sh"
    cat > "$hang_stub" <<'HANGEOF'
#!/usr/bin/env bash
# shift を落とした引数解析ループと同じ無限ループ。
while :; do :; done
HANGEOF
    chmod +x "$hang_stub"

    new_repo
    stage_claude_only_inconsistent
    capture_before

    saved_target="$target"
    saved_timeout="$run_timeout"
    target="$hang_stub"
    run_timeout=2
    expect_timeout=1
    run_check --auto-sync
    target="$saved_target"
    run_timeout="$saved_timeout"
    expect_timeout=0

    assert_eq "ハングがタイムアウトとして検出される" "1" "$run_timed_out"
    assert_eq "終了コードが timeout のもの（$timeout_exit_code）になる" \
        "$timeout_exit_code" "$run_rc"
    assert_no_sync "ハング時"
    assert_worktree_unchanged "ハング時"
else
    skip "timeout が使える状態でないため、ハング検出のケースを実行できなかった"
fi

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
