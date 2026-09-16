#!/usr/bin/env bash
# .claude/settings.json の SessionStart フック登録の検証
#
# 実行: scripts/test-session-start-hooks-config.sh
#
# 契約: 読み取り専用。settings.json にも作業ツリーにも一切書き込まない。
# 最後に settings.json のハッシュを実行前後で比較し、それを確かめる。
#
# 検証する内容:
#   - 作業方針フック（このリポジトリの .claude/scripts/hook-session-start-philosophy.sh）が
#     SessionStart に 1 件だけ登録され、matcher / type / command / timeout / statusMessage が仕様どおり
#   - 既存の herdr エントリと、PreToolUse / PostToolUse / Stop の内容が変わらない
#   - command が指すフック本体が、このリポジトリに実行権限付きで実在する
#   - settings.json のどこにも memory-mcp の clone のパスと MEMORY_MCP_PATH が現れない
#     （CLI の位置はシェル環境から供給する決定のため）
#   - リポジトリに .codex/hooks.json が無い（Codex がプロジェクト層として読むと、
#     ユーザー層の定義と重複して注入されるため）
#
# 比較は jq で正規化したうえで行い、キーの順序や無関係なキーには依存しない。
#
# 検査対象は環境変数で差し替えられる（充足可能性チェック・変異試験用）。
#   SESSION_START_HOOKS_REPO_ROOT : リポジトリルート（既定: このファイルの親ディレクトリ）
#   SESSION_START_HOOKS_SETTINGS  : settings.json（既定: <リポジトリルート>/.claude/settings.json）

set -uo pipefail

# jq の正規表現と出力の比較をロケールから独立させる。
export LC_ALL=C

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="${SESSION_START_HOOKS_REPO_ROOT:-$script_dir/..}"

# settings.json に書かれる文字列そのものと比較するため、~ は展開させない。
# shellcheck disable=SC2088
hook_command='~/.agents/.claude/scripts/hook-session-start-philosophy.sh'
hook_relative_path='.claude/scripts/hook-session-start-philosophy.sh'
hook_matcher='startup|clear|compact'
# settings.json に現れてはならない文字列。フック本体はこのリポジトリにあり、
# 共有メモリ CLI の位置は MEMORY_MCP_PATH としてシェル環境から供給する。
forbidden_in_settings='worktrees/github.com/s4kr4/memory-mcp
MEMORY_MCP_PATH'

herdr_entry='{
  "matcher": "*",
  "hooks": [
    {
      "type": "command",
      "command": "bash '"'"'/home/s4kr4/.claude/hooks/herdr-agent-state.sh'"'"' session",
      "timeout": 10
    }
  ]
}'

expected_pre_tool_use='[
  {
    "matcher": "Write|Edit",
    "hooks": [
      {
        "type": "command",
        "command": "~/.agents/.claude/scripts/hook-pre-boundary.sh",
        "statusMessage": "プロジェクト境界チェック中..."
      }
    ]
  }
]'

expected_post_tool_use='[
  {
    "matcher": "Write|Edit",
    "hooks": [
      {
        "type": "command",
        "command": "~/.agents/.claude/scripts/hook-post-secret-scan.sh",
        "statusMessage": "シークレットスキャン中..."
      }
    ]
  }
]'

expected_stop='[
  {
    "hooks": [
      {
        "type": "command",
        "command": "~/.agents/.claude/scripts/hook-stop-insight-logger.sh",
        "statusMessage": "Insightログ記録中..."
      },
      {
        "type": "command",
        "command": "~/.agents/.claude/scripts/hook-stop-skill-feedback.sh",
        "statusMessage": "スキルフィードバック自動記録中..."
      }
    ]
  }
]'

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
# 黙って pass させると「検証された」と誤読されるため必ず出力する。
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

assert_true() {
    local label="$1"
    if [ "$2" = "1" ]; then
        ok
    else
        fail "$label" "${3:-}"
    fi
}

fatal() {
    printf 'FATAL: %s\n' "$1" >&2
    exit 2
}

[ -d "$repo_root" ] || fatal "リポジトリルートがディレクトリではない: $repo_root"
repo_root="$(cd "$repo_root" && pwd -P)"
settings="${SESSION_START_HOOKS_SETTINGS:-$repo_root/.claude/settings.json}"
# command の "~/.agents/" はこのリポジトリを指す。
hook_file="$repo_root/$hook_relative_path"

# settings.json のバイト列。ファイルが無い場合も、比較が空文字列同士で
# 素通りしないよう明示的な値を返す。
snapshot_settings() {
    if [ -f "$settings" ]; then
        sha1sum < "$settings"
    else
        printf 'missing: %s\n' "$settings"
    fi
}

settings_before="$(snapshot_settings)"

# 前提の判定は名前の存在確認だけで済ませず、本テストが使う機能
# （any/2・正規表現・オブジェクトの順序非依存な等価比較）を実際に評価させる。
jq_usable=0
if command -v jq >/dev/null 2>&1; then
    probe="$(jq -n -c '[("a b" | test("\\S")), ("  " | test("\\S") | not),
        any([1, 2][]; . == 2), ({"a": 1, "b": 2} == {"b": 2, "a": 1})] | all' 2>/dev/null)"
    [ "$probe" = "true" ] && jq_usable=1
fi

# jq の失敗（JSON の破損など）は stderr ごと実測値に含め、期待値との不一致として現れるようにする。
jq_settings() {
    jq "$@" "$settings" 2>&1
}

canonical() {
    jq -S -c . <<<"$1"
}

# 作業方針フックを含む SessionStart エントリの一覧。$c は jq の変数。
# shellcheck disable=SC2016
entries_def='def entries:
    [.hooks.SessionStart[]?
     | select(type == "object")
     | select(any(.hooks[]?; type == "object" and .command == $c))];'

# ---------------------------------------------------------------------------
# 前提
# ---------------------------------------------------------------------------

start_test "前提: jq が使える"
if [ "$jq_usable" = "1" ]; then
    ok
else
    skip "jq が使える状態でないため、settings.json の内容を検査するケースをすべて実行できない"
fi

# ---------------------------------------------------------------------------
# settings.json の構造
# ---------------------------------------------------------------------------

start_test "settings.json がただ 1 つの JSON オブジェクトとして読める"
if [ "$jq_usable" = "1" ]; then
    assert_eq "JSON 値が 1 つで、オブジェクトである" "true" \
        "$(jq_settings -s -c 'length == 1 and (.[0] | type == "object")')"
else
    skip "jq が使えない"
fi

start_test "hooks.SessionStart が配列である"
if [ "$jq_usable" = "1" ]; then
    assert_eq "SessionStart の型" "array" "$(jq_settings -r '.hooks.SessionStart | type')"
else
    skip "jq が使えない"
fi

start_test "SessionStart に herdr エントリが内容を変えずにちょうど 1 件残っている"
if [ "$jq_usable" = "1" ]; then
    # $e は jq の変数。
    # shellcheck disable=SC2016
    assert_eq "herdr エントリと等しいエントリの件数" "1" \
        "$(jq_settings -r --argjson e "$herdr_entry" \
            '[.hooks.SessionStart[]? | select(. == $e)] | length')"
else
    skip "jq が使えない"
fi

# ---------------------------------------------------------------------------
# 作業方針フックの登録
# ---------------------------------------------------------------------------

start_test "SessionStart に作業方針フックのエントリがちょうど 1 件ある"
if [ "$jq_usable" = "1" ]; then
    assert_eq "作業方針フックを含むエントリの件数" "1" \
        "$(jq_settings -r --arg c "$hook_command" "$entries_def"' entries | length')"
else
    skip "jq が使えない"
fi

start_test "作業方針フックのエントリが仕様どおりの値を持つ"
if [ "$jq_usable" = "1" ]; then
    assert_eq "matcher が完全一致する" "$hook_matcher" \
        "$(jq_settings -r --arg c "$hook_command" "$entries_def"' entries[0].matcher')"
    assert_eq "hooks の要素が 1 つ" "1" \
        "$(jq_settings -r --arg c "$hook_command" "$entries_def"' entries[0].hooks | length')"
    assert_eq "type が command" "command" \
        "$(jq_settings -r --arg c "$hook_command" "$entries_def"' entries[0].hooks[0].type')"
    assert_eq "先頭の hook の command が作業方針フック" "$hook_command" \
        "$(jq_settings -r --arg c "$hook_command" "$entries_def"' entries[0].hooks[0].command')"
    assert_eq "timeout が数値の 10" "true" \
        "$(jq_settings -r --arg c "$hook_command" "$entries_def"'
            entries[0].hooks[0].timeout | type == "number" and . == 10')"
    assert_eq "statusMessage が空白以外の文字を含む文字列" "true" \
        "$(jq_settings -r --arg c "$hook_command" "$entries_def"'
            entries[0].hooks[0].statusMessage
            | if type == "string" then test("\\S") else false end')"
else
    skip "jq が使えない"
fi

start_test "作業方針フックの command が hooks 全体で 1 回だけ現れる"
# 別のイベントへの重複登録や、絶対パス表記での二重登録も二重注入になる。
if [ "$jq_usable" = "1" ]; then
    assert_eq "hook-session-start-philosophy.sh を含む command の件数" "1" \
        "$(jq_settings -r '[.hooks | .. | objects | .command? | strings
            | select(contains("hook-session-start-philosophy.sh"))] | length')"
else
    skip "jq が使えない"
fi

# ---------------------------------------------------------------------------
# 他の hooks の不変性
# ---------------------------------------------------------------------------

start_test "PreToolUse / PostToolUse / Stop の件数と内容が変わらない"
if [ "$jq_usable" = "1" ]; then
    assert_eq "PreToolUse の件数" "1" "$(jq_settings -r '.hooks.PreToolUse | length')"
    assert_eq "PreToolUse の内容" "$(canonical "$expected_pre_tool_use")" \
        "$(jq_settings -S -c '.hooks.PreToolUse')"
    assert_eq "PostToolUse の件数" "1" "$(jq_settings -r '.hooks.PostToolUse | length')"
    assert_eq "PostToolUse の内容" "$(canonical "$expected_post_tool_use")" \
        "$(jq_settings -S -c '.hooks.PostToolUse')"
    assert_eq "Stop の件数" "1" "$(jq_settings -r '.hooks.Stop | length')"
    assert_eq "Stop の内容" "$(canonical "$expected_stop")" \
        "$(jq_settings -S -c '.hooks.Stop')"
else
    skip "jq が使えない"
fi

# ---------------------------------------------------------------------------
# フック本体と Codex 側の構成
# ---------------------------------------------------------------------------

start_test "command が指すフック本体がリポジトリに実行権限付きで実在する"
assert_true "通常ファイルとして存在する" \
    "$([ -f "$hook_file" ] && echo 1 || echo 0)" "path: $hook_file"
assert_true "実行権限がある" \
    "$([ -f "$hook_file" ] && [ -x "$hook_file" ] && echo 1 || echo 0)" "path: $hook_file"

start_test "settings.json に memory-mcp の clone と MEMORY_MCP_PATH が現れない"
# jq を通さず生のバイト列を見る: hooks 以外のキーやコメント風の文字列も含めて、
# どこにも残っていないことを確かめる。
if [ -f "$settings" ]; then
    while IFS= read -r needle; do
        [ -n "$needle" ] || continue
        assert_eq "\"$needle\" を含む行の数" "0" \
            "$(grep -c -F -- "$needle" "$settings" || true)"
    done <<EOF
$forbidden_in_settings
EOF
else
    fail "settings.json が無い" "path: $settings"
fi

start_test "リポジトリに .codex/hooks.json が存在しない"
# -e はリンク切れのシンボリックリンクを見逃すため -L も確かめる。
assert_true ".codex/hooks.json が無い" \
    "$([ ! -e "$repo_root/.codex/hooks.json" ] && [ ! -L "$repo_root/.codex/hooks.json" ] && echo 1 || echo 0)" \
    "path: $repo_root/.codex/hooks.json"

# ---------------------------------------------------------------------------
# 事後スナップショット比較
# ---------------------------------------------------------------------------

start_test "テスト実行を通じて settings.json が変化しない"
assert_eq "settings.json のハッシュが実行前後で一致" "$settings_before" "$(snapshot_settings)"

# ---------------------------------------------------------------------------
printf '\n---------------------------------------------\n'
printf 'passed: %d  failed: %d  skipped: %d\n' "$pass_count" "$fail_count" "$skip_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0
