#!/usr/bin/env bash
# CLI 検出と残量表示の集約を、実環境へ到達しない PATH/HOME で検証する。
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${AGENT_RATE_STATUS_TARGET:-$script_dir/agent-rate-status}"
bash_bin="$(command -v bash)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
passed=0
failed=0
sequence=0

assert_eq() {
    if [[ "$2" == "$3" ]]; then
        passed=$((passed + 1))
    else
        printf 'FAIL: %s expected=[%s] actual=[%s]\n' "$1" "$2" "$3"
        failed=$((failed + 1))
    fi
}

# CLI は存在確認だけの対象。誤って起動すればマーカーを残す。
make_cli() {
    cat > "$case_dir/bin/$1" <<'STUB'
#!/bin/bash
printf 'executed\n' >> "$HOME/cli-executed"
exit 99
STUB
    chmod +x "$case_dir/bin/$1"
}

make_reader() {
    cat > "$case_dir/bin/$1-rate-status" <<'STUB'
#!/bin/bash
name=${0##*/}
printf '%s\n' "$name" >> "$HOME/readers-called"
case "$name" in
    claude-*) value=$CLAUDE_VALUE; status=$CLAUDE_STATUS ;;
    codex-*) value=$CODEX_VALUE; status=$CODEX_STATUS ;;
esac
printf '%s' "$value"
[[ "$status" == 0 ]] || printf 'reader failed\n' >&2
exit "$status"
STUB
    chmod +x "$case_dir/bin/$1-rate-status"
}

# run_case label CLI一覧 reader一覧 Claude値 Codex値 Claude終了 Codex終了 期待値 呼出一覧
run_case() {
    local label="$1" cli_list="$2" reader_list="$3" name rc actual calls
    sequence=$((sequence + 1))
    case_dir="$work/case$sequence"
    mkdir -p "$case_dir/bin" "$case_dir/home"
    for name in $cli_list; do make_cli "$name"; done
    for name in $reader_list; do make_reader "$name"; done
    env -i PATH="$case_dir/bin" HOME="$case_dir/home" \
        CODEX_HOME="$case_dir/codex" XDG_CACHE_HOME="$case_dir/cache" \
        XDG_CONFIG_HOME="$case_dir/config" TMPDIR="$case_dir" \
        CLAUDE_VALUE="$4" CODEX_VALUE="$5" CLAUDE_STATUS="$6" CODEX_STATUS="$7" \
        "$bash_bin" "$target" > "$case_dir/out" 2> "$case_dir/err"
    rc=$?
    actual="$(cat "$case_dir/out")"
    calls=""
    [[ ! -f "$case_dir/home/readers-called" ]] || calls="$(cat "$case_dir/home/readers-called")"
    assert_eq "$label: exit 0" 0 "$rc"
    assert_eq "$label: stdout" "$8" "$actual"
    assert_eq "$label: stderr" "" "$(cat "$case_dir/err")"
    assert_eq "$label: CLI を起動しない" no "$([[ -e "$case_dir/home/cli-executed" ]] && echo yes || echo no)"
    assert_eq "$label: 必要な reader だけ呼ぶ" "$9" "$calls"
}

claude_value='5h:79% 7d:94%'
codex_value='5h:50% 7d:75%*'
run_case '両CLIなし' '' 'claude codex' "$claude_value" "$codex_value" 0 0 '' ''
run_case 'Claudeのみ' claude 'claude codex' "$claude_value" "$codex_value" 0 0 \
    '⚡ Claude 5h:79% 7d:94%' claude-rate-status
run_case 'Codexのみ' codex 'claude codex' "$claude_value" "$codex_value" 0 0 \
    '⚡ Codex 5h:50% 7d:75%*' codex-rate-status
run_case '両方表示' 'claude codex' 'claude codex' "$claude_value" "$codex_value" 0 0 \
    '⚡ Claude 5h:79% 7d:94% | Codex 5h:50% 7d:75%*' $'claude-rate-status\ncodex-rate-status'
run_case '両方データなし' 'claude codex' 'claude codex' '' '' 0 0 '' \
    $'claude-rate-status\ncodex-rate-status'
run_case 'Claude失敗でもCodex表示' 'claude codex' 'claude codex' "$claude_value" "$codex_value" 1 0 \
    '⚡ Codex 5h:50% 7d:75%*' $'claude-rate-status\ncodex-rate-status'
run_case 'Codex失敗でもClaude表示' 'claude codex' 'claude codex' "$claude_value" "$codex_value" 0 1 \
    '⚡ Claude 5h:79% 7d:94%' $'claude-rate-status\ncodex-rate-status'
run_case 'reader不在' 'claude codex' '' '' '' 0 0 '' ''

printf 'passed: %d failed: %d\n' "$passed" "$failed"
[[ "$failed" == 0 ]]
