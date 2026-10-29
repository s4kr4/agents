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
# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
target="${DEPLOY_SH_TARGET:-$script_dir/deploy.sh}"
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
    for d in "$real_home/.claude" "$real_home/.claude/skills/" "$real_home/.codex/skills" "$real_home/.local/bin"; do
        find "$d" -maxdepth 1 -printf '%y %p %l\n' 2>/dev/null | sort
    done
}

# 実リポジトリの skills 直下のスナップショット。旧構成では実 HOME の
# ~/.claude/skills がここを指すため、誤ってリンク先へ書き込んだ場合はここに現れる。
real_repo="$(cd "$script_dir/.." && pwd)"
snapshot_real_repo() {
    local d
    for d in "$real_repo/.claude/skills" "$real_repo/.codex/skills"; do
        find "$d" -maxdepth 1 -printf '%y %p %l\n' 2>/dev/null | sort
    done
}

real_repo_before="$(snapshot_real_repo)"

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
# skills 配下には、skill（直下に SKILL.md を持つディレクトリ）と、
# skill ではないエントリ（実行時データのディレクトリ・下位にだけ SKILL.md がある
# ディレクトリ・通常ファイル）を混在させる。
mkdir -p \
    "$agents_stub/.claude/agents" \
    "$agents_stub/.claude/skills/gamma-skill" \
    "$agents_stub/.claude/skills/delta skill" \
    "$agents_stub/.claude/skills/_tracker" \
    "$agents_stub/.claude/skills/synced/abc123" \
    "$agents_stub/.claude/rules" \
    "$agents_stub/.codex/skills/alpha-skill" \
    "$agents_stub/.codex/skills/beta-skill" \
    "$agents_stub/.codex/skills/notes" \
    "$agents_stub/.codex/skills/nested/inner" \
    "$agents_stub/scripts"

printf 'stub AGENTS.md\n'      > "$agents_stub/.claude/AGENTS.md"
printf '{"stub": true}\n'      > "$agents_stub/.claude/settings.json"
printf 'stub agent\n'          > "$agents_stub/.claude/agents/stub-agent.md"
printf 'stub gamma\n'          > "$agents_stub/.claude/skills/gamma-skill/SKILL.md"
printf 'stub delta\n'          > "$agents_stub/.claude/skills/delta skill/SKILL.md"
printf '{"stub": "tracker"}\n' > "$agents_stub/.claude/skills/_tracker/insights.jsonl"
printf 'stub synced\n'         > "$agents_stub/.claude/skills/synced/abc123/SKILL.md"
printf 'stub skill\n'          > "$agents_stub/.claude/skills/stub-skill.md"
printf 'stub rule\n'           > "$agents_stub/.claude/rules/stub-rule.md"
printf 'stub alpha\n'          > "$agents_stub/.codex/skills/alpha-skill/SKILL.md"
printf 'stub beta\n'           > "$agents_stub/.codex/skills/beta-skill/SKILL.md"
printf 'stub notes\n'          > "$agents_stub/.codex/skills/notes/README.md"
printf 'stub nested\n'         > "$agents_stub/.codex/skills/nested/inner/SKILL.md"
printf 'stub readme\n'         > "$agents_stub/.codex/skills/README.md"

# PATH 解決で起動されることを確かめるためのスタブ。
# 実 claude-rate-status は起動しない（本テストの対象外）。
rate_status_src="$agents_stub/scripts/claude-rate-status"
cat > "$rate_status_src" <<'STUBEOF'
#!/bin/sh
echo deploy-test-rate-status-ok
STUBEOF
chmod +x "$rate_status_src"

codex_rate_status_src="$agents_stub/scripts/codex-rate-status"
cat > "$codex_rate_status_src" <<'STUBEOF'
#!/bin/sh
echo deploy-test-codex-rate-status-ok
STUBEOF
chmod +x "$codex_rate_status_src"

agent_rate_status_src="$agents_stub/scripts/agent-rate-status"
cat > "$agent_rate_status_src" <<'STUBEOF'
#!/bin/sh
echo deploy-test-agent-rate-status-ok
STUBEOF
chmod +x "$agent_rate_status_src"

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
agents="$agents_stub"

# new_home: ケースごとに独立した HOME stub を用意する
new_home() {
    home_seq=$((home_seq + 1))
    home="$work/home$home_seq"
    mkdir -p "$home"
    agents="$agents_stub"
}

# new_agents: 共有の AGENTSPATH stub を複製し、このケースだけで使う。
# HOME 側にリポジトリを指すリンクを置くケースは、誤ってリンク先へ書き込まれても
# 後続ケースの stub を汚さないよう複製側で実行する。
new_agents() {
    agents="$work/agents$home_seq"
    cp -a "$agents_stub" "$agents"
}

# run_deploy: 隔離ガードを通してから deploy.sh を起動する
run_deploy() {
    fatal_unless_isolated "$home" "$agents"
    : > "$out_file"
    : > "$err_file"
    env -i \
        PATH="$deploy_path" \
        HOME="$home" \
        AGENTSPATH="$agents" \
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

# assert_real_dir <label> <path>
# シンボリックリンク経由のディレクトリを実ディレクトリと取り違えないよう、-L を先に見る。
assert_real_dir() {
    local label="$1" path="$2"
    if [ -L "$path" ]; then
        fail "$label: 実ディレクトリである" "path : $path
type : symlink -> $(readlink "$path")"
    elif [ -d "$path" ]; then
        ok
    else
        fail "$label: 実ディレクトリである" "path : $path
type : $(if [ -e "$path" ]; then echo file; else echo missing; fi)"
    fi
}

# assert_absent <label> <path>
# 壊れたリンクは -e で偽になるため、-L と合わせて「何も無い」ことを確かめる。
assert_absent() {
    local label="$1" path="$2"
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        ok
    else
        fail "$label" "path : $path
type : $(if [ -L "$path" ]; then echo "symlink -> $(readlink "$path")"; elif [ -d "$path" ]; then echo directory; else echo file; fi)"
    fi
}

# list_entries <dir>: 直下のエントリ名を C ロケールの順に '|' 区切りで返す。
# skill 名は空白を含みうるため、空白区切りにしない。
list_entries() {
    find "$1" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null | LC_ALL=C sort | paste -sd'|' -
}

# describe_entries <dir>: 直下のエントリの種別・名前・リンク先
describe_entries() {
    find "$1" -mindepth 1 -maxdepth 1 -printf '%y %f -> %l\n' 2>/dev/null | LC_ALL=C sort
}

# snapshot_tree <dir>: 配下全体の種別・相対パス・リンク先と通常ファイルの内容。
# ディレクトリのサイズはエントリ数でファイルシステムごとに揺れるため含めない。
snapshot_tree() {
    find "$1" -printf '%y %P -> %l\n' 2>/dev/null | LC_ALL=C sort
    (cd "$1" 2>/dev/null && find . -type f -exec md5sum {} + | LC_ALL=C sort)
}

# assert_snapshot_collected <label> <snapshot>
# 空同士の比較は常に一致するため、比較の前に収集できていることを確かめる。
assert_snapshot_collected() {
    local label="$1" snapshot="$2"
    assert_true "$label: スナップショットを収集できている" \
        "$([ -n "$snapshot" ] && echo 1 || echo 0)"
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
assert_eq ".local/bin の中身は個別と集約の残量コマンド" \
    "agent-rate-status claude-rate-status codex-rate-status" \
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

start_test "codex-rate-status を配置し PATH から起動できる"
assert_symlink_to "Codex のリンク" "$home/.local/bin/codex-rate-status" "$codex_rate_status_src"
assert_stdout_mentions "Codex の配置対象が報告される" "$home/.local/bin/codex-rate-status"
resolved_out="$(env -i PATH="$home/.local/bin" "$bash_bin" -c 'codex-rate-status' 2>"$work/codex-resolve-err.txt")"
resolved_rc=$?
assert_eq "Codex のベース名起動が exit 0" "0" "$resolved_rc"
assert_eq "Codex のスタブ出力が得られる" "deploy-test-codex-rate-status-ok" "$resolved_out"
assert_eq "Codex の stderr は空" "" "$(cat "$work/codex-resolve-err.txt")"

start_test "agent-rate-status を配置し PATH から起動できる"
assert_symlink_to "集約コマンドのリンク" "$home/.local/bin/agent-rate-status" "$agent_rate_status_src"
assert_stdout_mentions "集約コマンドの配置が報告される" "$home/.local/bin/agent-rate-status"
resolved_out="$(env -i PATH="$home/.local/bin" "$bash_bin" -c 'agent-rate-status' 2>"$work/agent-resolve-err.txt")"
resolved_rc=$?
assert_eq "集約コマンドの起動が exit 0" "0" "$resolved_rc"
assert_eq "集約コマンドのスタブ出力が得られる" "deploy-test-agent-rate-status-ok" "$resolved_out"
assert_eq "集約コマンドの stderr は空" "" "$(cat "$work/agent-resolve-err.txt")"

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
# Claude Code はユーザー単位では ~/.claude/CLAUDE.md しか読まないため、
# リンク名は CLAUDE.md のまま、リンク元をリポジトリの .claude/AGENTS.md にする。
assert_symlink_to "CLAUDE.md"     "$home/.claude/CLAUDE.md"     "$agents_stub/.claude/AGENTS.md"
assert_symlink_to "settings.json" "$home/.claude/settings.json" "$agents_stub/.claude/settings.json"
assert_symlink_to "agents"        "$home/.claude/agents"        "$agents_stub/.claude/agents"
assert_real_dir   "skills"        "$home/.claude/skills"
assert_symlink_to "rules"         "$home/.claude/rules"         "$agents_stub/.claude/rules"

start_test "Codex skills は直下に SKILL.md を持つディレクトリだけがリンクされる"
assert_symlink_to "alpha-skill" "$home/.codex/skills/alpha-skill" "$agents_stub/.codex/skills/alpha-skill"
assert_symlink_to "beta-skill"  "$home/.codex/skills/beta-skill"  "$agents_stub/.codex/skills/beta-skill"
assert_eq ".codex/skills の中身は SKILL.md を持つディレクトリと一致" \
    "alpha-skill beta-skill" \
    "$(cd "$home/.codex/skills" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ' | sed 's/ $//')"
assert_absent "SKILL.md を持たないディレクトリはリンクされない" "$home/.codex/skills/notes"
assert_absent "下位にだけ SKILL.md があるディレクトリはリンクされない" "$home/.codex/skills/nested"
assert_absent "通常ファイルはリンクされない" "$home/.codex/skills/README.md"

start_test "リンク経由で設定ファイルの内容が読める"
assert_eq "CLAUDE.md 経由で AGENTS.md の内容が読める" "stub AGENTS.md" "$(cat "$home/.claude/CLAUDE.md" 2>/dev/null)"
assert_eq "settings.json の内容が読める" '{"stub": true}' "$(cat "$home/.claude/settings.json")"
assert_eq "agents 配下が読める"          "stub agent"     "$(cat "$home/.claude/agents/stub-agent.md")"
assert_eq "codex skill 配下が読める"     "stub alpha"     "$(cat "$home/.codex/skills/alpha-skill/SKILL.md")"
assert_eq "claude skill 配下が読める"    "stub gamma"     "$(cat "$home/.claude/skills/gamma-skill/SKILL.md" 2>/dev/null)"
assert_eq "空白を含む claude skill 配下が読める" "stub delta" "$(cat "$home/.claude/skills/delta skill/SKILL.md" 2>/dev/null)"

start_test "旧リンク元を指す CLAUDE.md のリンクは AGENTS.md へ張り直す"
# リポジトリ側で .claude/CLAUDE.md を AGENTS.md へ改名すると、以前の配置で
# 張ったリンクは解決できない状態で残る。stub に .claude/CLAUDE.md は無いため、
# このリンクはその壊れたリンクと同じ形になる。
new_home
mkdir -p "$home/.claude"
ln -s "$agents_stub/.claude/CLAUDE.md" "$home/.claude/CLAUDE.md"
run_deploy
assert_eq "旧リンク既存でも exit 0" "0" "$run_rc"
assert_eq "旧リンク既存でも stderr は空" "" "$run_err"
assert_symlink_to "旧リンク既存: CLAUDE.md" "$home/.claude/CLAUDE.md" "$agents_stub/.claude/AGENTS.md"
assert_stdout_mentions "旧リンク既存: 配置対象として報告される" "$home/.claude/CLAUDE.md"
assert_true "旧リンクは .backup へ退避されない" \
    "$([ ! -e "$home/.claude/CLAUDE.md.backup" ] && [ ! -L "$home/.claude/CLAUDE.md.backup" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.claude" -maxdepth 1 2>/dev/null | sort | tr '\n' ' ')"
assert_eq "旧リンク既存: CLAUDE.md 経由で AGENTS.md の内容が読める" "stub AGENTS.md" "$(cat "$home/.claude/CLAUDE.md" 2>/dev/null)"

start_test "既存の CLAUDE.md 実ファイルは .backup へ退避してから AGENTS.md へリンクする"
# 手編集された内容を想定し、CRLF と末尾改行なしを含めてバイト単位の保持を確かめる。
new_home
mkdir -p "$home/.claude"
printf '# hand-written CLAUDE.md\r\nlocal rule\r\nno trailing newline' > "$home/.claude/CLAUDE.md"
cp "$home/.claude/CLAUDE.md" "$work/handwritten-claude-md-original"
run_deploy
assert_eq "実ファイル退避を伴っても exit 0" "0" "$run_rc"
assert_eq "実ファイル退避を伴っても stderr は空" "" "$run_err"
assert_symlink_to "実ファイル退避後: CLAUDE.md" "$home/.claude/CLAUDE.md" "$agents_stub/.claude/AGENTS.md"
assert_stdout_mentions "実ファイル退避後: 退避が報告される" "Backed up: $home/.claude/CLAUDE.md"
assert_true "退避先 CLAUDE.md.backup が通常ファイルとして存在する" \
    "$([ -f "$home/.claude/CLAUDE.md.backup" ] && [ ! -L "$home/.claude/CLAUDE.md.backup" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.claude" -maxdepth 1 2>/dev/null | sort | tr '\n' ' ')"
if cmp -s "$work/handwritten-claude-md-original" "$home/.claude/CLAUDE.md.backup"; then
    ok
else
    fail "退避された CLAUDE.md の内容がバイト単位で保持される" \
        "backup: $(od -c "$home/.claude/CLAUDE.md.backup" 2>/dev/null | head -5)"
fi

start_test "デプロイ対象が HOME 配下だけに限定される"
new_home
agents_before="$(find "$agents_stub" | sort)"
run_deploy
assert_eq "AGENTSPATH 側が変更されない" "$agents_before" "$(find "$agents_stub" | sort)"
assert_true "HOME 直下に想定外のエントリが作られない" \
    "$([ "$(cd "$home" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ' | sed 's/ $//')" = ".claude .codex .local" ] && echo 1 || echo 0)" \
    "entries: $(cd "$home" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ')"

# --- 条件8: Claude skills の skill 単位の配置
claude_skills_expected="delta skill|gamma-skill"

start_test "\$HOME/.claude/skills は実ディレクトリになり、直下に SKILL.md を持つディレクトリだけがリンクされる"
new_home
run_deploy
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_real_dir "\$HOME/.claude/skills" "$home/.claude/skills"
assert_symlink_to "gamma-skill" "$home/.claude/skills/gamma-skill" "$agents_stub/.claude/skills/gamma-skill"
assert_symlink_to "空白を含む skill 名" "$home/.claude/skills/delta skill" "$agents_stub/.claude/skills/delta skill"
assert_eq "\$HOME/.claude/skills の中身は skill のリンクだけ" "$claude_skills_expected" "$(list_entries "$home/.claude/skills")"
assert_absent "実行時データのディレクトリ（_tracker）はリンクされない" "$home/.claude/skills/_tracker"
assert_absent "下位にだけ SKILL.md がある synced はリンクされない" "$home/.claude/skills/synced"
assert_absent "通常ファイルはリンクされない" "$home/.claude/skills/stub-skill.md"
assert_stdout_mentions "gamma-skill の配置が報告される" "Linked: $home/.claude/skills/gamma-skill"
assert_stdout_mentions "空白を含む skill の配置が報告される" "Linked: $home/.claude/skills/delta skill"
assert_absent "skills.backup が作られない" "$home/.claude/skills.backup"

start_test "旧構成のリポジトリを指す ~/.claude/skills は実ディレクトリへ置き換わり、リポジトリ側は変わらない"
# 旧構成では ~/.claude/skills がリポジトリの .claude/skills を指している。
# リンクを外さずに書き込むと、リポジトリ側にリンクが作られてしまう。
new_home
new_agents
mkdir -p "$home/.claude"
ln -s "$agents/.claude/skills" "$home/.claude/skills"
agents_before="$(snapshot_tree "$agents")"
assert_snapshot_collected "旧構成" "$agents_before"
run_deploy
assert_eq "旧構成: exit 0" "0" "$run_rc"
assert_eq "旧構成: stderr は空" "" "$run_err"
assert_stdout_mentions "旧構成: リンクを外したことが報告される" "Unlinked: $home/.claude/skills"
assert_real_dir "旧構成: ~/.claude/skills" "$home/.claude/skills"
assert_symlink_to "旧構成: gamma-skill" "$home/.claude/skills/gamma-skill" "$agents/.claude/skills/gamma-skill"
assert_symlink_to "旧構成: 空白を含む skill 名" "$home/.claude/skills/delta skill" "$agents/.claude/skills/delta skill"
assert_eq "旧構成: ~/.claude/skills の中身は skill のリンクだけ" "$claude_skills_expected" "$(list_entries "$home/.claude/skills")"
assert_eq "旧構成: リポジトリ側（_tracker・synced を含む）が変更されない" "$agents_before" "$(snapshot_tree "$agents")"
assert_absent "旧構成: リンクは skills.backup へ退避されない" "$home/.claude/skills.backup"

start_test "リポジトリ外を指す ~/.claude/skills は外して実ディレクトリにし、リンク先は変えない"
new_home
elsewhere="$work/elsewhere$home_seq"
mkdir -p "$elsewhere/other-skill"
printf 'other skill\r\n' > "$elsewhere/other-skill/SKILL.md"
printf 'loose file' > "$elsewhere/loose.txt"
elsewhere_before="$(snapshot_tree "$elsewhere")"
assert_snapshot_collected "他を指すリンク" "$elsewhere_before"
mkdir -p "$home/.claude"
ln -s "$elsewhere" "$home/.claude/skills"
run_deploy
assert_eq "他を指すリンク: exit 0" "0" "$run_rc"
assert_eq "他を指すリンク: stderr は空" "" "$run_err"
assert_stdout_mentions "他を指すリンク: リンクを外したことが報告される" "Unlinked: $home/.claude/skills"
assert_real_dir "他を指すリンク: ~/.claude/skills" "$home/.claude/skills"
assert_eq "他を指すリンク: ~/.claude/skills の中身は skill のリンクだけ" "$claude_skills_expected" "$(list_entries "$home/.claude/skills")"
assert_eq "他を指すリンク: リンク先の中身が変更されない" "$elsewhere_before" "$(snapshot_tree "$elsewhere")"
assert_absent "他を指すリンク: skills.backup へ退避されない" "$home/.claude/skills.backup"

start_test "壊れたリンクの ~/.claude/skills は外して実ディレクトリにする"
# 別マシンの AGENTSPATH を指したまま残った旧構成のリンクを想定する。
new_home
mkdir -p "$home/.claude"
ln -s "/nonexistent/other-machine/.agents/.claude/skills" "$home/.claude/skills"
run_deploy
assert_eq "壊れたリンク: exit 0" "0" "$run_rc"
assert_eq "壊れたリンク: stderr は空" "" "$run_err"
assert_stdout_mentions "壊れたリンク: リンクを外したことが報告される" "Unlinked: $home/.claude/skills"
assert_real_dir "壊れたリンク: ~/.claude/skills" "$home/.claude/skills"
assert_symlink_to "壊れたリンク: gamma-skill" "$home/.claude/skills/gamma-skill" "$agents_stub/.claude/skills/gamma-skill"
assert_eq "壊れたリンク: ~/.claude/skills の中身は skill のリンクだけ" "$claude_skills_expected" "$(list_entries "$home/.claude/skills")"
assert_absent "壊れたリンク: skills.backup へ退避されない" "$home/.claude/skills.backup"

start_test "実ファイルの ~/.claude/skills は skills.backup へ退避してから実ディレクトリにする"
new_home
mkdir -p "$home/.claude"
printf 'hand-written\r\nno trailing newline' > "$home/.claude/skills"
cp "$home/.claude/skills" "$work/skills-file-original$home_seq"
run_deploy
assert_eq "実ファイル: exit 0" "0" "$run_rc"
assert_eq "実ファイル: stderr は空" "" "$run_err"
assert_real_dir "実ファイル: ~/.claude/skills" "$home/.claude/skills"
assert_symlink_to "実ファイル: gamma-skill" "$home/.claude/skills/gamma-skill" "$agents_stub/.claude/skills/gamma-skill"
assert_stdout_mentions "実ファイル: 退避が報告される" "Backed up: $home/.claude/skills"
assert_true "実ファイル: skills.backup が通常ファイルとして存在する" \
    "$([ -f "$home/.claude/skills.backup" ] && [ ! -L "$home/.claude/skills.backup" ] && echo 1 || echo 0)" \
    "tree: $(find "$home/.claude" -maxdepth 1 2>/dev/null | sort | tr '\n' ' ')"
if cmp -s "$work/skills-file-original$home_seq" "$home/.claude/skills.backup"; then
    ok
else
    fail "実ファイル: 退避された内容がバイト単位で保持される" \
        "backup: $(od -c "$home/.claude/skills.backup" 2>/dev/null | head -5)"
fi

start_test "実ディレクトリの ~/.claude/skills はそのまま使い、ローカルのエントリと外部リンクを残す"
new_home
new_agents
outside="$work/outside$home_seq"
mkdir -p "$outside/ext-skill"
printf 'ext skill\n' > "$outside/ext-skill/SKILL.md"
skills_dir="$home/.claude/skills"
mkdir -p "$skills_dir/my-local" "$skills_dir/synced/abc123" "$skills_dir/_tracker"
printf 'local skill\r\nno trailing newline' > "$skills_dir/my-local/SKILL.md"
# ローカル skill の中にあるリンクは、リポジトリ内の壊れた先を指していても片付けの対象外。
ln -s "$agents/.claude/skills/removed-skill" "$skills_dir/my-local/inner-link"
printf 'synced skill\n' > "$skills_dir/synced/abc123/SKILL.md"
printf '{"local": "tracker"}\n' > "$skills_dir/_tracker/insights.jsonl"
printf 'notes\n' > "$skills_dir/notes.txt"
ln -s "$outside/ext-skill" "$skills_dir/outside-link"
ln -s "/nonexistent/other-machine/.agents/.claude/skills/old-skill" "$skills_dir/outside-broken"
# 別ツールの skills 配下は Claude 側の片付けの対象外。
ln -s "$agents/.codex/skills/gone-skill" "$skills_dir/cross-tool"
local_before="$(describe_entries "$skills_dir")
$(snapshot_tree "$skills_dir")"
assert_snapshot_collected "ローカルのエントリ" "$(describe_entries "$skills_dir")"
outside_before="$(snapshot_tree "$outside")"
run_deploy
assert_eq "実ディレクトリ: exit 0" "0" "$run_rc"
assert_eq "実ディレクトリ: stderr は空" "" "$run_err"
assert_real_dir "実ディレクトリ: ~/.claude/skills" "$skills_dir"
assert_symlink_to "実ディレクトリ: gamma-skill" "$skills_dir/gamma-skill" "$agents/.claude/skills/gamma-skill"
assert_symlink_to "実ディレクトリ: 空白を含む skill 名" "$skills_dir/delta skill" "$agents/.claude/skills/delta skill"
assert_eq "実ディレクトリ: skill 以外のエントリが実行前のまま残る" \
    "$local_before" \
    "$(describe_entries "$skills_dir" | grep -vF -e "l gamma-skill -> " -e "l delta skill -> ")
$(snapshot_tree "$skills_dir" | grep -vF -e "l gamma-skill -> " -e "l delta skill -> ")"
assert_eq "実ディレクトリ: 外部リンクの先が変更されない" "$outside_before" "$(snapshot_tree "$outside")"
assert_absent "実ディレクトリ: skills.backup へ退避されない" "$home/.claude/skills.backup"

start_test "\$HOME/.claude/skills 直下の skill 名にある実ディレクトリ・実ファイルは .backup へ退避してからリンクする"
new_home
skills_dir="$home/.claude/skills"
mkdir -p "$skills_dir/gamma-skill"
printf 'hand edited\r\nno trailing newline' > "$skills_dir/gamma-skill/SKILL.md"
printf 'delta as a file\r\n' > "$skills_dir/delta skill"
cp -a "$skills_dir/gamma-skill" "$work/gamma-original$home_seq"
cp "$skills_dir/delta skill" "$work/delta-original$home_seq"
run_deploy
assert_eq "skill 名の実体: exit 0" "0" "$run_rc"
assert_eq "skill 名の実体: stderr は空" "" "$run_err"
assert_real_dir "skill 名の実体: ~/.claude/skills" "$skills_dir"
assert_symlink_to "skill 名の実体: gamma-skill" "$skills_dir/gamma-skill" "$agents_stub/.claude/skills/gamma-skill"
assert_symlink_to "skill 名の実体: 空白を含む skill 名" "$skills_dir/delta skill" "$agents_stub/.claude/skills/delta skill"
assert_eq "skill 名の実体: 退避先と並ぶ" \
    "delta skill|delta skill.backup|gamma-skill|gamma-skill.backup" \
    "$(list_entries "$skills_dir")"
assert_stdout_mentions "skill 名の実体: ディレクトリの退避が報告される" "Backed up: $skills_dir/gamma-skill"
assert_stdout_mentions "skill 名の実体: ファイルの退避が報告される" "Backed up: $skills_dir/delta skill"
assert_eq "skill 名の実体: 退避したディレクトリの中身が保持される" \
    "$(snapshot_tree "$work/gamma-original$home_seq")" \
    "$(snapshot_tree "$skills_dir/gamma-skill.backup")"
assert_real_dir "skill 名の実体: gamma-skill.backup" "$skills_dir/gamma-skill.backup"
if cmp -s "$work/delta-original$home_seq" "$skills_dir/delta skill.backup"; then
    ok
else
    fail "skill 名の実体: 退避したファイルの内容がバイト単位で保持される" \
        "backup: $(od -c "$skills_dir/delta skill.backup" 2>/dev/null | head -5)"
fi

start_test "Codex skills の skill 名にあるリポジトリ外へのリンクは張り直し、退避しない"
new_home
outside="$work/outside$home_seq"
mkdir -p "$outside/beta-skill" "$home/.codex/skills"
printf 'other beta\n' > "$outside/beta-skill/SKILL.md"
outside_before="$(snapshot_tree "$outside")"
ln -s "$outside/beta-skill" "$home/.codex/skills/beta-skill"
run_deploy
assert_eq "skill 名のリンク: exit 0" "0" "$run_rc"
assert_symlink_to "skill 名のリンク: beta-skill" "$home/.codex/skills/beta-skill" "$agents_stub/.codex/skills/beta-skill"
assert_absent "skill 名のリンク: beta-skill.backup が作られない" "$home/.codex/skills/beta-skill.backup"
assert_eq "skill 名のリンク: 元のリンク先が変更されない" "$outside_before" "$(snapshot_tree "$outside")"

# --- 条件9: 古いリンクの片付け
start_test "\$HOME/.claude/skills 直下でリポジトリの skill を指さなくなったリンクを取り除き、報告する"
new_home
new_agents
skills_dir="$home/.claude/skills"
mkdir -p "$skills_dir"
ln -s "$agents/.claude/skills/gamma-skill" "$skills_dir/gamma-skill"
ln -s "$agents/.claude/skills/removed-skill" "$skills_dir/stale-broken"
ln -s "$agents/.claude/skills/removed skill" "$skills_dir/stale with space"
ln -s "$agents/.claude/skills/_tracker" "$skills_dir/stale-tracker"
ln -s "$agents/.claude/skills/synced" "$skills_dir/stale-nested"
ln -s "$agents/.claude/skills/stub-skill.md" "$skills_dir/stale-file"
# skills 直下ではなく下位を指すリンクは、その先が SKILL.md を持っていても取り除く。
ln -s "$agents/.claude/skills/synced/abc123" "$skills_dir/stale-deep"
# skills 直下の skill を指す別名のリンクは残す。
ln -s "$agents/.claude/skills/gamma-skill" "$skills_dir/alias"
# 片付けの対象は skills 直下だけ。~/.claude 直下のリンクは触らない。
ln -s "$agents/.claude/skills/removed-skill" "$home/.claude/stale-outside-skills"
agents_before="$(snapshot_tree "$agents")"
run_deploy
assert_eq "片付け: exit 0" "0" "$run_rc"
assert_eq "片付け: stderr は空" "" "$run_err"
assert_real_dir "片付け: ~/.claude/skills" "$skills_dir"
for stale in stale-broken "stale with space" stale-tracker stale-nested stale-file stale-deep; do
    assert_absent "片付け: $stale が取り除かれる" "$skills_dir/$stale"
    assert_stdout_mentions "片付け: $stale の除去が報告される" "$skills_dir/$stale"
    assert_absent "片付け: $stale は退避されない" "$skills_dir/$stale.backup"
done
assert_symlink_to "片付け: skill を指す別名のリンクは残る" "$skills_dir/alias" "$agents/.claude/skills/gamma-skill"
assert_absent "片付け: 別名のリンクは退避されない" "$skills_dir/alias.backup"
assert_eq "片付け: ~/.claude/skills の中身は skill のリンクと別名のリンクだけ" \
    "alias|$claude_skills_expected" \
    "$(list_entries "$skills_dir")"
assert_symlink_to "片付け: skills 直下以外のリンクは残る" \
    "$home/.claude/stale-outside-skills" "$agents/.claude/skills/removed-skill"
assert_eq "片付け: リポジトリ側（リンク先の _tracker・synced を含む）が変更されない" "$agents_before" "$(snapshot_tree "$agents")"
assert_absent "片付け: skills.backup へ退避されない" "$home/.claude/skills.backup"

start_test "\$HOME/.codex/skills 直下でリポジトリの skill を指さなくなったリンクを取り除き、ローカルのエントリは残す"
new_home
new_agents
outside="$work/outside$home_seq"
mkdir -p "$outside/ext-skill"
printf 'ext\n' > "$outside/ext-skill/SKILL.md"
codex_dir="$home/.codex/skills"
mkdir -p "$codex_dir/.system/builtin"
printf 'system skill\n' > "$codex_dir/.system/builtin/SKILL.md"
ln -s "$agents/.codex/skills/alpha-skill" "$codex_dir/alpha-skill"
ln -s "$agents/.codex/skills/removed-skill" "$codex_dir/stale-codex"
ln -s "$agents/.codex/skills/notes" "$codex_dir/stale-notes"
ln -s "$agents/.codex/skills/nested/inner" "$codex_dir/stale-deep"
ln -s "$agents/.codex/skills/beta-skill" "$codex_dir/codex-alias"
ln -s "$outside/ext-skill" "$codex_dir/codex-outside"
ln -s "/nonexistent/other-machine/.agents/.codex/skills/old-skill" "$codex_dir/codex-outside-broken"
ln -s "$agents/.codex/skills/removed-skill" "$home/.codex/stale-outside-skills"
system_before="$(snapshot_tree "$codex_dir/.system")"
agents_before="$(snapshot_tree "$agents")"
run_deploy
assert_eq "Codex 片付け: exit 0" "0" "$run_rc"
assert_eq "Codex 片付け: stderr は空" "" "$run_err"
for stale in stale-codex stale-notes stale-deep; do
    assert_absent "Codex 片付け: $stale が取り除かれる" "$codex_dir/$stale"
    assert_stdout_mentions "Codex 片付け: $stale の除去が報告される" "$codex_dir/$stale"
done
assert_symlink_to "Codex 片付け: skill を指す別名のリンクは残る" "$codex_dir/codex-alias" "$agents/.codex/skills/beta-skill"
assert_eq "Codex 片付け: ~/.codex/skills の中身" \
    ".system|alpha-skill|beta-skill|codex-alias|codex-outside|codex-outside-broken" \
    "$(list_entries "$codex_dir")"
assert_eq "Codex 片付け: ドットで始まる実ディレクトリが変更されない" "$system_before" "$(snapshot_tree "$codex_dir/.system")"
assert_symlink_to "Codex 片付け: リポジトリ外を指すリンクは残る" "$codex_dir/codex-outside" "$outside/ext-skill"
assert_symlink_to "Codex 片付け: リポジトリ外を指す壊れたリンクは残る" \
    "$codex_dir/codex-outside-broken" "/nonexistent/other-machine/.agents/.codex/skills/old-skill"
assert_symlink_to "Codex 片付け: skills 直下以外のリンクは残る" \
    "$home/.codex/stale-outside-skills" "$agents/.codex/skills/removed-skill"
assert_eq "Codex 片付け: リポジトリ側が変更されない" "$agents_before" "$(snapshot_tree "$agents")"

# check_cleanup_edges <tool> <skill>
# 片付けの境界: ドットで始まる名前のリンクも片付けの対象にし、リンク先が
# skills 直下ではないもの（skills そのもの・名前の接頭辞だけが共通する兄弟）は残す。
check_cleanup_edges() {
    local tool="$1" skill="$2" dir stale
    new_home
    new_agents
    dir="$home/.$tool/skills"
    mkdir -p "$dir" "$agents/.$tool/skills-archive/foo"
    printf 'archived skill\n' > "$agents/.$tool/skills-archive/foo/SKILL.md"
    ln -s "$agents/.$tool/skills/removed-dot" "$dir/.stale-dot"
    ln -s "$agents/.$tool/skills/removed-dot2" "$dir/..stale-dot2"
    ln -s "$agents/.$tool/skills/$skill" "$dir/.alias-dot"
    ln -s "$agents/.$tool/skills" "$dir/whole"
    ln -s "$agents/.$tool/skills-archive/foo" "$dir/sibling"
    ln -s "$agents/.$tool/skills-archive/missing" "$dir/sibling-broken"
    agents_before="$(snapshot_tree "$agents")"
    run_deploy
    assert_eq "$tool 境界: exit 0" "0" "$run_rc"
    assert_eq "$tool 境界: stderr は空" "" "$run_err"
    for stale in .stale-dot ..stale-dot2; do
        assert_absent "$tool 境界: $stale が取り除かれる" "$dir/$stale"
        assert_stdout_mentions "$tool 境界: $stale の除去が報告される" "$dir/$stale"
    done
    assert_symlink_to "$tool 境界: ドットで始まる別名のリンクは残る" "$dir/.alias-dot" "$agents/.$tool/skills/$skill"
    assert_symlink_to "$tool 境界: skills そのものを指すリンクは残る" "$dir/whole" "$agents/.$tool/skills"
    assert_symlink_to "$tool 境界: 接頭辞だけが共通する兄弟を指すリンクは残る" \
        "$dir/sibling" "$agents/.$tool/skills-archive/foo"
    assert_symlink_to "$tool 境界: 接頭辞だけが共通する兄弟を指す壊れたリンクは残る" \
        "$dir/sibling-broken" "$agents/.$tool/skills-archive/missing"
    assert_eq "$tool 境界: リポジトリ側が変更されない" "$agents_before" "$(snapshot_tree "$agents")"
}

start_test "\$HOME/.claude/skills の片付けはドットで始まる名前も対象にし、skills 直下の外を指すリンクは残す"
check_cleanup_edges claude gamma-skill

start_test "\$HOME/.codex/skills の片付けはドットで始まる名前も対象にし、skills 直下の外を指すリンクは残す"
check_cleanup_edges codex alpha-skill

# --- 条件10: skills の配置の冪等性
start_test "skills の配置を 3 回実行しても結果が変わらず、エントリも .backup も増えない"
new_home
new_agents
skills_dir="$home/.claude/skills"
codex_dir="$home/.codex/skills"
mkdir -p "$skills_dir/gamma-skill" "$skills_dir/my-local" "$codex_dir/.system"
printf 'hand edited\n' > "$skills_dir/gamma-skill/SKILL.md"
printf 'local\n' > "$skills_dir/my-local/SKILL.md"
printf 'system\n' > "$codex_dir/.system/SKILL.md"
ln -s "$agents/.claude/skills/removed-skill" "$skills_dir/stale-broken"
ln -s "$agents/.codex/skills/removed-skill" "$codex_dir/stale-codex"
agents_before="$(snapshot_tree "$agents")"
run_deploy
first_rc="$run_rc"
first_state="$(describe_entries "$skills_dir")
$(describe_entries "$codex_dir")
$(describe_entries "$home/.claude")"
run_deploy
second_rc="$run_rc"
second_err="$run_err"
second_state="$(describe_entries "$skills_dir")
$(describe_entries "$codex_dir")
$(describe_entries "$home/.claude")"
run_deploy
third_state="$(describe_entries "$skills_dir")
$(describe_entries "$codex_dir")
$(describe_entries "$home/.claude")"
assert_eq "冪等: 1 回目 exit 0" "0" "$first_rc"
assert_eq "冪等: 2 回目 exit 0" "0" "$second_rc"
assert_eq "冪等: 3 回目 exit 0" "0" "$run_rc"
assert_eq "冪等: 2 回目の stderr は空" "" "$second_err"
assert_eq "冪等: 3 回目の stderr は空" "" "$run_err"
assert_snapshot_collected "冪等" "$first_state"
assert_eq "冪等: 2 回目の結果が 1 回目と一致" "$first_state" "$second_state"
assert_eq "冪等: 3 回目の結果が 1 回目と一致" "$first_state" "$third_state"
assert_eq "冪等: ~/.claude/skills の中身" \
    "delta skill|gamma-skill|gamma-skill.backup|my-local" \
    "$(list_entries "$skills_dir")"
assert_eq "冪等: ~/.codex/skills の中身" ".system|alpha-skill|beta-skill" "$(list_entries "$codex_dir")"
assert_eq "冪等: リポジトリ側が変更されない" "$agents_before" "$(snapshot_tree "$agents")"

# --- 条件7: 実 HOME を汚染していない
start_test "テスト実行を通じて実 HOME が変化しない"
assert_eq "実 HOME のリンク構成が実行前後で一致" "$real_home_before" "$(snapshot_real_home)"
assert_snapshot_collected "実リポジトリの skills" "$real_repo_before"
assert_eq "実リポジトリの skills 直下が実行前後で一致" "$real_repo_before" "$(snapshot_real_repo)"

# ---------------------------------------------------------------------------
printf '\n---------------------------------------------\n'
printf 'passed: %d  failed: %d\n' "$pass_count" "$fail_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0
