#!/usr/bin/env bash
# deploy.sh の振る舞いテスト
#
# 実行: scripts/test-deploy.sh
#
# deploy.sh は $HOME 配下にシンボリックリンクを張る破壊的なスクリプト。
# 本テストは env -i で HOME をテンポラリの stub に差し替えて起動し、
# 実 HOME には一切触れない。AGENTSPATH も同様に、テストが組み立てた
# ダミーツリーへ差し替えるため、実リポジトリの内容には依存しない。
#
# 実 HOME への到達経路が無いことは、起動前のガード（fatal_unless_isolated）と
# 全ケース実行後の実 HOME スナップショット比較の両方で確認する。

set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$script_dir/deploy.sh"
bash_bin="$(command -v bash)"
real_home="$HOME"

work="$(mktemp -d)"
trap 'chmod -R u+rwX "$work" 2>/dev/null; rm -rf "$work"' EXIT

pass_count=0
fail_count=0
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

assert_true() {
    local label="$1"
    if [ "$2" = "1" ]; then
        ok
    else
        fail "$label" "${3:-}"
    fi
}

# assert_symlink_to <label> <link-path> <expected-target>
# 「シンボリックリンクであること」と「リンク先文字列」を分けて検証する。
# 実ファイルが置かれた場合に readlink が空を返して素通りするのを防ぐ。
assert_symlink_to() {
    local label="$1" link="$2" expected="$3"
    if [ ! -L "$link" ]; then
        fail "$label: シンボリックリンクである" \
            "path : $link
type : $(if [ -d "$link" ]; then echo directory; elif [ -e "$link" ]; then echo file; else echo missing; fi)"
        return
    fi
    ok
    assert_eq "$label: リンク先" "$expected" "$(readlink "$link")"
}

# ---------------------------------------------------------------------------
# 隔離ガード（実 HOME 汚染の防止）
# ---------------------------------------------------------------------------

fatal() {
    printf 'FATAL: %s\n' "$1" >&2
    exit 2
}

# テストが deploy.sh へ渡す HOME / AGENTSPATH が
# 実 HOME・実リポジトリから確実に切り離されていることを起動前に検証する。
fatal_unless_isolated() {
    local home_dir="$1" agents_dir="$2"

    [ -n "$home_dir" ] || fatal "HOME stub が空"
    [ -n "$agents_dir" ] || fatal "AGENTSPATH stub が空"

    case "$home_dir" in
        "$work"/*) ;;
        *) fatal "HOME stub が作業ディレクトリ外を指している: $home_dir" ;;
    esac
    case "$agents_dir" in
        "$work"/*) ;;
        *) fatal "AGENTSPATH stub が作業ディレクトリ外を指している: $agents_dir" ;;
    esac

    [ "$home_dir" != "$real_home" ] || fatal "HOME stub が実 HOME と一致している"
    [ "$agents_dir" != "$script_dir/.." ] || fatal "AGENTSPATH stub が実リポジトリを指している"

    case "$home_dir" in
        "$real_home"/*) fatal "HOME stub が実 HOME 配下にある: $home_dir" ;;
    esac
    case "$real_home"/ in
        "$home_dir"/*) fatal "実 HOME が HOME stub 配下にある: $home_dir" ;;
    esac
}

# 実 HOME のうち deploy.sh が触りうる場所のスナップショット。
# 名前・種別・リンク先だけを見るため、無関係なファイル更新では揺れない。
snapshot_real_home() {
    local d
    for d in "$real_home/.claude" "$real_home/.codex/skills" "$real_home/.local/bin"; do
        find "$d" -maxdepth 1 -printf '%y %p %l\n' 2>/dev/null | sort
    done
}

real_home_before="$(snapshot_real_home)"

# ---------------------------------------------------------------------------
# 実行環境の構築
# ---------------------------------------------------------------------------

# deploy.sh はディレクトリ作成・リンク張り替え・バックアップを行うため、
# 書き込み系コマンドを含む現実的な PATH で実行する。
deploy_path="/usr/bin:/bin"
for cmd in mkdir mv ln rm basename; do
    if ! env -i PATH="$deploy_path" "$bash_bin" -c "command -v $cmd" >/dev/null 2>&1; then
        fatal "required command not found in $deploy_path: $cmd"
    fi
done

agents_stub="$work/agents"
mkdir -p \
    "$agents_stub/.claude/agents" \
    "$agents_stub/.claude/skills" \
    "$agents_stub/.claude/rules" \
    "$agents_stub/.codex/skills/alpha-skill" \
    "$agents_stub/.codex/skills/beta-skill" \
    "$agents_stub/scripts"

printf 'stub CLAUDE.md\n'      > "$agents_stub/.claude/CLAUDE.md"
printf '{"stub": true}\n'      > "$agents_stub/.claude/settings.json"
printf 'stub agent\n'          > "$agents_stub/.claude/agents/stub-agent.md"
printf 'stub skill\n'          > "$agents_stub/.claude/skills/stub-skill.md"
printf 'stub rule\n'           > "$agents_stub/.claude/rules/stub-rule.md"
printf 'stub alpha\n'          > "$agents_stub/.codex/skills/alpha-skill/SKILL.md"
printf 'stub beta\n'           > "$agents_stub/.codex/skills/beta-skill/SKILL.md"

# PATH 解決で起動されることを確かめるためのスタブ。
# 実 claude-rate-status は起動しない（本テストの対象外）。
rate_status_src="$agents_stub/scripts/claude-rate-status"
cat > "$rate_status_src" <<'STUBEOF'
#!/bin/sh
echo deploy-test-rate-status-ok
STUBEOF
chmod +x "$rate_status_src"

rate_status_rel=".local/bin/claude-rate-status"

# ---------------------------------------------------------------------------
# 実行ヘルパー
# ---------------------------------------------------------------------------

out_file="$work/stdout.raw"
err_file="$work/stderr.raw"
run_out=""
run_err=""
run_rc=0
home_seq=0
home=""

# new_home: ケースごとに独立した HOME stub を用意する
new_home() {
    home_seq=$((home_seq + 1))
    home="$work/home$home_seq"
    mkdir -p "$home"
}

# run_deploy: 隔離ガードを通してから deploy.sh を起動する
run_deploy() {
    fatal_unless_isolated "$home" "$agents_stub"
    : > "$out_file"
    : > "$err_file"
    env -i \
        PATH="$deploy_path" \
        HOME="$home" \
        AGENTSPATH="$agents_stub" \
        "$bash_bin" "$target" > "$out_file" 2> "$err_file"
    run_rc=$?
    run_out="$(cat "$out_file")"
    run_err="$(cat "$err_file")"
}

# assert_stdout_mentions <label> <substring>
# deploy.sh は配置した各パスを標準出力へ報告する。既に正しいリンクが
# ある場合でも「処理対象として通った」ことを、状態だけでなく報告からも確認する。
assert_stdout_mentions() {
    local label="$1" needle="$2"
    if grep -qF -- "$needle" "$out_file"; then
        ok
    else
        fail "$label" "needle: [$needle]
stdout: $(cat "$out_file")"
    fi
}

# assert_rate_status_link <prefix>
# claude-rate-status のリンクが仕様どおり張られていることを検証する
assert_rate_status_link() {
    local prefix="$1"
    assert_symlink_to "$prefix: $rate_status_rel" "$home/$rate_status_rel" "$rate_status_src"
}

# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------

# --- 条件1: claude-rate-status を $HOME/.local/bin へリンクする
start_test "\$HOME/.local/bin/claude-rate-status が AGENTSPATH の実体を指すリンクになる"
new_home
run_deploy
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_rate_status_link "初回実行"
assert_stdout_mentions "初回実行: 配置対象として報告される" "$home/$rate_status_rel"
assert_true "リンク先が実体として解決できる" \
    "$([ -e "$home/$rate_status_rel" ] && echo 1 || echo 0)" \
    "readlink: $(readlink "$home/$rate_status_rel" 2>/dev/null)"

# --- 条件2: $HOME/.local/bin の作成
start_test "\$HOME/.local 自体が存在しない場合に .local/bin を作成する"
new_home
assert_true "前提: .local が存在しない" \
    "$([ ! -e "$home/.local" ] && echo 1 || echo 0)"
run_deploy
assert_eq ".local 不在でも exit 0" "0" "$run_rc"
assert_true ".local/bin がディレクトリとして作られる" \
    "$([ -d "$home/.local/bin" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.local" 2>/dev/null | sort | tr '\n' ' ')"
assert_rate_status_link ".local 不在"

start_test "\$HOME/.local はあるが bin が無い場合に bin を作成する"
new_home
mkdir -p "$home/.local/share"
run_deploy
assert_eq "bin 不在でも exit 0" "0" "$run_rc"
assert_true ".local/bin がディレクトリとして作られる" \
    "$([ -d "$home/.local/bin" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.local" 2>/dev/null | sort | tr '\n' ' ')"
assert_rate_status_link "bin 不在"
assert_true "既存の .local/share が残っている" \
    "$([ -d "$home/.local/share" ] && echo 1 || echo 0)"

start_test "既に空の \$HOME/.local/bin がある場合も成功する"
new_home
mkdir -p "$home/.local/bin"
run_deploy
assert_eq "既存 bin でも exit 0" "0" "$run_rc"
assert_eq "既存 bin でも stderr は空" "" "$run_err"
assert_rate_status_link "既存 bin"

# --- 条件3: 冪等性
start_test "2 回連続で実行してもリンクが壊れない（冪等）"
new_home
run_deploy
first_rc="$run_rc"
run_deploy
assert_eq "1 回目 exit 0" "0" "$first_rc"
assert_eq "2 回目 exit 0" "0" "$run_rc"
assert_eq "2 回目も stderr は空" "" "$run_err"
assert_rate_status_link "2 回目"
assert_true "2 回目で .backup が作られない" \
    "$([ ! -e "$home/$rate_status_rel.backup" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.local" 2>/dev/null | sort | tr '\n' ' ')"

start_test "3 回実行しても .local/bin の内容が増殖しない（冪等）"
new_home
run_deploy
run_deploy
run_deploy
assert_eq "3 回目 exit 0" "0" "$run_rc"
assert_rate_status_link "3 回目"
assert_eq ".local/bin の中身は claude-rate-status のみ" \
    "claude-rate-status" \
    "$(cd "$home/.local/bin" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ' | sed 's/ $//')"

# --- 条件4: リンク経由で実行できる
start_test "張られたリンク経由で claude-rate-status を起動できる"
new_home
run_deploy
exec_out="$(env -i PATH="$home/.local/bin" "$home/$rate_status_rel" 2>"$work/exec-err.txt")"
exec_rc=$?
assert_eq "リンク経由の実行が exit 0" "0" "$exec_rc"
assert_eq "リンク経由でスタブの出力が得られる" "deploy-test-rate-status-ok" "$exec_out"
assert_eq "リンク経由実行の stderr は空" "" "$(cat "$work/exec-err.txt")"

start_test "PATH に \$HOME/.local/bin だけを置いてベース名で解決できる"
# herdr の tab_bar_right はベース名を PATH 解決で起動する。
# 絶対パス指定を排したあとに機能するのはこの経路のため、独立して固定する。
resolved_out="$(env -i PATH="$home/.local/bin" "$bash_bin" -c 'claude-rate-status' 2>"$work/resolve-err.txt")"
resolved_rc=$?
assert_eq "ベース名起動が exit 0" "0" "$resolved_rc"
assert_eq "ベース名起動でスタブの出力が得られる" "deploy-test-rate-status-ok" "$resolved_out"
assert_eq "ベース名起動の stderr は空" "" "$(cat "$work/resolve-err.txt")"

start_test "リポジトリ内の scripts/claude-rate-status に実行権がある"
# リンクを張っても実体に実行権が無ければ PATH 解決経路は成立しない。
assert_true "scripts/claude-rate-status が実行可能" \
    "$([ -x "$script_dir/claude-rate-status" ] && echo 1 || echo 0)" \
    "mode: $(find "$script_dir/claude-rate-status" -maxdepth 0 -printf '%M\n' 2>/dev/null)"

# --- 条件5: 既存の実ファイル・リンクの扱い（link_file の契約）
start_test "既存の実ファイルは .backup へ退避してからリンクを張る"
new_home
mkdir -p "$home/.local/bin"
printf '#!/bin/sh\necho hand-written\n' > "$home/$rate_status_rel"
chmod +x "$home/$rate_status_rel"
cp "$home/$rate_status_rel" "$work/handwritten-original"
run_deploy
assert_eq "退避を伴っても exit 0" "0" "$run_rc"
assert_rate_status_link "実ファイル退避後"
assert_true "退避先 .backup が存在する" \
    "$([ -f "$home/$rate_status_rel.backup" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.local" 2>/dev/null | sort | tr '\n' ' ')"
if cmp -s "$work/handwritten-original" "$home/$rate_status_rel.backup"; then
    ok
else
    fail "退避された内容がバイト単位で保持される" \
        "backup: $(cat "$home/$rate_status_rel.backup" 2>/dev/null)"
fi

start_test "既に正しいリンクがある場合は張り直して壊さない"
new_home
mkdir -p "$home/.local/bin"
ln -s "$rate_status_src" "$home/$rate_status_rel"
run_deploy
assert_eq "正しいリンク既存でも exit 0" "0" "$run_rc"
assert_rate_status_link "正しいリンク既存"
assert_stdout_mentions "正しいリンク既存: 配置対象として報告される" "$home/$rate_status_rel"
assert_true "既存リンクは .backup へ退避されない" \
    "$([ ! -e "$home/$rate_status_rel.backup" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.local" 2>/dev/null | sort | tr '\n' ' ')"

start_test "別マシン由来の壊れたリンクがあっても張り直す"
# AGENTSPATH がマシンごとに異なると、以前の配置で張ったリンクが
# 解決できない状態で残る。手編集ではなく実運用で起こりうる非正規形。
new_home
mkdir -p "$home/.local/bin"
ln -s "/nonexistent/other-machine/scripts/claude-rate-status" "$home/$rate_status_rel"
run_deploy
assert_eq "壊れたリンク既存でも exit 0" "0" "$run_rc"
assert_rate_status_link "壊れたリンク既存"
assert_stdout_mentions "壊れたリンク既存: 配置対象として報告される" "$home/$rate_status_rel"
assert_true "壊れたリンクは .backup へ退避されない" \
    "$([ ! -e "$home/$rate_status_rel.backup" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.local" 2>/dev/null | sort | tr '\n' ' ')"

# --- 条件6: 既存の配置対象が壊れていないこと（退行防止）
start_test "Claude Code の設定リンクが従来どおり張られる"
new_home
run_deploy
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_symlink_to "CLAUDE.md"     "$home/.claude/CLAUDE.md"     "$agents_stub/.claude/CLAUDE.md"
assert_symlink_to "settings.json" "$home/.claude/settings.json" "$agents_stub/.claude/settings.json"
assert_symlink_to "agents"        "$home/.claude/agents"        "$agents_stub/.claude/agents"
assert_symlink_to "skills"        "$home/.claude/skills"        "$agents_stub/.claude/skills"
assert_symlink_to "rules"         "$home/.claude/rules"         "$agents_stub/.claude/rules"

start_test "Codex skills のリンクが従来どおり張られる"
assert_symlink_to "alpha-skill" "$home/.codex/skills/alpha-skill" "$agents_stub/.codex/skills/alpha-skill"
assert_symlink_to "beta-skill"  "$home/.codex/skills/beta-skill"  "$agents_stub/.codex/skills/beta-skill"
assert_eq ".codex/skills の中身はソースと一致" \
    "alpha-skill beta-skill" \
    "$(cd "$home/.codex/skills" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ' | sed 's/ $//')"

start_test "リンク経由で設定ファイルの内容が読める"
assert_eq "CLAUDE.md の内容が読める"     "stub CLAUDE.md" "$(cat "$home/.claude/CLAUDE.md")"
assert_eq "settings.json の内容が読める" '{"stub": true}' "$(cat "$home/.claude/settings.json")"
assert_eq "agents 配下が読める"          "stub agent"     "$(cat "$home/.claude/agents/stub-agent.md")"
assert_eq "codex skill 配下が読める"     "stub alpha"     "$(cat "$home/.codex/skills/alpha-skill/SKILL.md")"

start_test "デプロイ対象が HOME 配下だけに限定される"
new_home
agents_before="$(find "$agents_stub" | sort)"
run_deploy
assert_eq "AGENTSPATH 側が変更されない" "$agents_before" "$(find "$agents_stub" | sort)"
assert_true "HOME 直下に想定外のエントリが作られない" \
    "$([ "$(cd "$home" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ' | sed 's/ $//')" = ".claude .codex .local" ] && echo 1 || echo 0)" \
    "entries: $(cd "$home" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ')"

# --- 条件7: 実 HOME を汚染していない
start_test "テスト実行を通じて実 HOME が変化しない"
assert_eq "実 HOME のリンク構成が実行前後で一致" "$real_home_before" "$(snapshot_real_home)"

# ---------------------------------------------------------------------------
printf '\n---------------------------------------------\n'
printf 'passed: %d  failed: %d\n' "$pass_count" "$fail_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0
