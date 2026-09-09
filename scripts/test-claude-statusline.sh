#!/usr/bin/env bash
# claude-statusline の振る舞いテスト
#
# 実行: scripts/test-claude-statusline.sh
#
# 対象スクリプトは env -i で起動し、PATH には本テストが用意した
# シンボリックリンクだけを含める。date は意図的に含めない
# （時刻取得は bash 組み込み printf '%(%s)T' を使う）。

set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$script_dir/claude-statusline"
bash_bin="$(command -v bash)"

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

assert_match() {
    local label="$1" pattern="$2" actual="$3"
    if printf '%s' "$actual" | grep -Eq -- "$pattern"; then
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

# ---------------------------------------------------------------------------
# 実行環境の構築
# ---------------------------------------------------------------------------

real_bin="$work/realbin"
dot_stub="$work/dotstub"
home_stub="$work/home"
proj_dir="$work/myproj"

mkdir -p "$real_bin" "$dot_stub/etc/lib" "$home_stub" "$proj_dir"

# 対象スクリプトが使用するコマンドだけを PATH に露出させる。
# mkdir / mv / mktemp / rm はキャッシュのアトミック書き込み用。
# date は含めない（bash 組み込み printf '%(%s)T' の使用を強制する）。
for cmd in cat jq basename git whoami hostname awk grep wc tr sed dirname mkdir mv mktemp rm; do
    path="$(command -v "$cmd" 2>/dev/null)"
    if [ -z "$path" ]; then
        printf 'FATAL: required command not found: %s\n' "$cmd" >&2
        exit 2
    fi
    ln -sf "$path" "$real_bin/$cmd"
done

if [ -e "$real_bin/date" ]; then
    printf 'FATAL: date must not be exposed to the subject\n' >&2
    exit 2
fi

# util.zsh スタブ。実物の ink / ink256 は text を printf のフォーマット文字列として
# 扱う（対象スクリプトが "%%" でエスケープしているのはこのため）ので、
# 色付けを除いた同じセマンティクスを再現する。
cat > "$dot_stub/etc/lib/util.zsh" <<'UTILEOF'
ink() {
    if [ "$#" -eq 2 ]; then
        printf -- "$2"
    else
        printf -- "$1"
    fi
}

ink256() {
    if [ "$#" -eq 2 ]; then
        printf -- "$2"
    else
        printf -- "$1"
    fi
}
UTILEOF

# ---------------------------------------------------------------------------
# 入力 JSON
# ---------------------------------------------------------------------------

# 正規形: 42000/200000 = 21%, 5h 21.4% used -> 79% rem, 7d 6.0% used -> 94% rem
json_baseline() {
    cat <<EOF
{
  "workspace": { "project_dir": "$proj_dir", "current_dir": "$proj_dir" },
  "model": { "display_name": "Opus 5" },
  "context_window": {
    "context_window_size": 200000,
    "current_usage": {
      "input_tokens": 30000,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 7000
    }
  },
  "rate_limits": {
    "five_hour": { "used_percentage": 21.4 },
    "seven_day": { "used_percentage": 6.0 }
  }
}
EOF
}

# 境界値: .5 の四捨五入。21.5 -> 22 (rem 78), 6.5 -> 7 (rem 93)
json_half_rounding() {
    cat <<EOF
{
  "workspace": { "project_dir": "$proj_dir", "current_dir": "$proj_dir" },
  "model": { "display_name": "Opus 5" },
  "context_window": {
    "context_window_size": 200000,
    "current_usage": {
      "input_tokens": 30000,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 7000
    }
  },
  "rate_limits": {
    "five_hour": { "used_percentage": 21.5 },
    "seven_day": { "used_percentage": 6.5 }
  }
}
EOF
}

# 異常系: rate_limits 欠落
json_no_rate_limits() {
    cat <<EOF
{
  "workspace": { "project_dir": "$proj_dir", "current_dir": "$proj_dir" },
  "model": { "display_name": "Opus 5" },
  "context_window": {
    "context_window_size": 200000,
    "current_usage": {
      "input_tokens": 30000,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 7000
    }
  }
}
EOF
}

# 異常系: 新規セッション（current_usage なし）
json_no_usage() {
    cat <<EOF
{
  "workspace": { "project_dir": "$proj_dir", "current_dir": "$proj_dir" },
  "model": { "display_name": "Opus 5" },
  "context_window": { "context_window_size": 200000 },
  "rate_limits": {
    "five_hour": { "used_percentage": 21.4 },
    "seven_day": { "used_percentage": 6.0 }
  }
}
EOF
}

# 異常系: context_window_size = 0（ゼロ除算ガード）
json_zero_context() {
    cat <<EOF
{
  "workspace": { "project_dir": "$proj_dir", "current_dir": "$proj_dir" },
  "model": { "display_name": "Opus 5" },
  "context_window": {
    "context_window_size": 0,
    "current_usage": {
      "input_tokens": 0,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0
    }
  },
  "rate_limits": {
    "five_hour": { "used_percentage": 21.4 },
    "seven_day": { "used_percentage": 6.0 }
  }
}
EOF
}

# ---------------------------------------------------------------------------
# 実行ヘルパー
# ---------------------------------------------------------------------------

run_out=""
run_err=""
run_rc=0

# run_statusline_with_path <PATH> <json-producer> [KEY=VALUE ...]
run_statusline_with_path() {
    local path_value="$1"
    shift
    local producer="$1"
    shift

    local infile="$work/input.json"
    "$producer" > "$infile"

    local errfile="$work/stderr.txt"
    run_out="$(env -i \
        PATH="$path_value" \
        HOME="$home_stub" \
        DOTPATH="$dot_stub" \
        "$@" \
        "$bash_bin" "$target" < "$infile" 2>"$errfile")"
    run_rc=$?
    run_err="$(cat "$errfile")"
}

# run_statusline <json-producer> [KEY=VALUE ...]
run_statusline() {
    run_statusline_with_path "$real_bin" "$@"
}

# new_cache_root: 未使用のキャッシュルート（XDG_CACHE_HOME 相当）を作り、変数 root へ設定する
cache_root_seq=0
new_cache_root() {
    cache_root_seq=$((cache_root_seq + 1))
    root="$work/xdg$cache_root_seq"
    mkdir -p "$root"
}

# cache_file <cache-root>
cache_file() {
    printf '%s/claude-statusline/rate-limits' "$1"
}

# assert_file_line <label> <file> <exact-line>
assert_file_line() {
    local label="$1" file="$2" line="$3"
    if [ -f "$file" ] && grep -Fxq -- "$line" "$file"; then
        ok
    else
        fail "$label" "want line: [$line]
file    : [$file]
content : [$( [ -f "$file" ] && cat "$file" )]"
    fi
}

expected_line1="$(whoami)@$(hostname -s) │ myproj │ Opus 5"
expected_line2_baseline="⛁ 42.0k/200.0k (21.0%) │ ⚡ 5h:79% rem 7d:94% rem"

# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------

# --- 条件1: 正規形入力でレートリミットキャッシュが所定パスへ書き出される
start_test "正規形入力でレートリミットキャッシュが XDG_CACHE_HOME 配下へ書き出される"
new_cache_root
printf -v t_before '%(%s)T' -1
run_statusline json_baseline XDG_CACHE_HOME="$root"
printf -v t_after '%(%s)T' -1
cf="$(cache_file "$root")"
assert_true "キャッシュファイルが \$XDG_CACHE_HOME/claude-statusline/rate-limits に生成される" \
    "$([ -f "$cf" ] && echo 1 || echo 0)" \
    "not found: $cf
tree: $(find "$root" 2>/dev/null | sort | tr '\n' ' ')"
assert_file_line "five_hour_remaining=79" "$cf" "five_hour_remaining=79"
assert_file_line "seven_day_remaining=94" "$cf" "seven_day_remaining=94"
cached_updated_at="$(sed -n 's/^updated_at=//p' "$cf" 2>/dev/null | head -1)"
assert_match "updated_at が epoch 秒（整数）" '^[0-9]+$' "$cached_updated_at"
assert_true "updated_at が実行時刻の近傍" \
    "$([ -n "$cached_updated_at" ] && [ "$cached_updated_at" -ge "$t_before" ] && [ "$cached_updated_at" -le "$t_after" ] 2>/dev/null && echo 1 || echo 0)" \
    "updated_at=[$cached_updated_at] window=[$t_before..$t_after]"

# --- 条件1(書式): printf エスケープ由来の壊れた値が混入しない
start_test "キャッシュの値に % 由来の壊れた表記が混入しない"
assert_true "キャッシュに % 文字が含まれない" \
    "$( ! grep -q '%' "$cf" && echo 1 || echo 0)" \
    "content: $(cat "$cf" 2>/dev/null)"

# --- 条件2: キャッシュディレクトリが存在しない場合は書き込み側が作成する
start_test "キャッシュディレクトリが存在しない場合 statusLine 側が作成する"
new_cache_root
rm -rf "$root"
run_statusline json_baseline XDG_CACHE_HOME="$root"
cf="$(cache_file "$root")"
assert_true "ディレクトリごと作成される" \
    "$([ -d "$root/claude-statusline" ] && echo 1 || echo 0)" \
    "tree: $(find "$root" 2>/dev/null | sort | tr '\n' ' ')"
assert_true "キャッシュファイルが生成される" "$([ -f "$cf" ] && echo 1 || echo 0)" "not found: $cf"

# --- 条件3: 一時ファイルが残らない（アトミック書き込みの後始末）
start_test "キャッシュ書き込み後に一時ファイルが残らない"
assert_eq "キャッシュディレクトリの中身は rate-limits のみ" \
    "rate-limits" "$(ls -A "$root/claude-statusline" 2>/dev/null | sort | tr '\n' ' ' | sed 's/ $//')"
assert_eq "キャッシュルート直下は claude-statusline のみ" \
    "claude-statusline" "$(ls -A "$root" 2>/dev/null | sort | tr '\n' ' ' | sed 's/ $//')"

# --- 条件4: XDG_CACHE_HOME 未設定時は \$HOME/.cache へ書き出す
start_test "XDG_CACHE_HOME 未設定時は \$HOME/.cache 配下へ書き出す"
rm -rf "$home_stub/.cache"
run_statusline json_baseline
home_cf="$home_stub/.cache/claude-statusline/rate-limits"
assert_true "\$HOME/.cache/claude-statusline/rate-limits が生成される" \
    "$([ -f "$home_cf" ] && echo 1 || echo 0)" \
    "not found: $home_cf
tree: $(find "$home_stub" 2>/dev/null | sort | tr '\n' ' ')"
assert_file_line "five_hour_remaining=79（HOME フォールバック）" "$home_cf" "five_hour_remaining=79"
assert_file_line "seven_day_remaining=94（HOME フォールバック）" "$home_cf" "seven_day_remaining=94"

# --- 条件5: 丸め境界。used_percentage が .5 のとき四捨五入して残を算出する
start_test "used_percentage が .5 のとき四捨五入した残をキャッシュへ書く"
new_cache_root
run_statusline json_half_rounding XDG_CACHE_HOME="$root"
cf="$(cache_file "$root")"
assert_file_line "21.5% used -> five_hour_remaining=78" "$cf" "five_hour_remaining=78"
assert_file_line "6.5% used -> seven_day_remaining=93" "$cf" "seven_day_remaining=93"

# --- 条件6: rate_limits 欠落時はキャッシュを作らない
start_test "rate_limits が欠落した入力ではキャッシュを作らない"
new_cache_root
run_statusline json_no_rate_limits XDG_CACHE_HOME="$root"
cf="$(cache_file "$root")"
assert_true "キャッシュファイルが作られない" "$([ ! -e "$cf" ] && echo 1 || echo 0)" \
    "unexpected content: $(cat "$cf" 2>/dev/null)"

# --- 条件6(不変): rate_limits 欠落時は既存キャッシュを上書きしない
start_test "rate_limits が欠落した入力では既存キャッシュを上書きしない"
new_cache_root
cf="$(cache_file "$root")"
mkdir -p "$root/claude-statusline"
stale_content='five_hour_remaining=11
seven_day_remaining=22
updated_at=1000000000'
printf '%s\n' "$stale_content" > "$cf"
run_statusline json_no_rate_limits XDG_CACHE_HOME="$root"
assert_eq "既存キャッシュの内容が変化しない" "$stale_content" "$(cat "$cf")"

# --- 条件7: 有効な入力では既存キャッシュを新しい値で更新する
start_test "有効な rate_limits があれば既存キャッシュを新しい値で更新する"
new_cache_root
cf="$(cache_file "$root")"
mkdir -p "$root/claude-statusline"
printf 'five_hour_remaining=11\nseven_day_remaining=22\nupdated_at=1000000000\n' > "$cf"
run_statusline json_baseline XDG_CACHE_HOME="$root"
assert_file_line "five_hour_remaining が 79 に更新される" "$cf" "five_hour_remaining=79"
assert_file_line "seven_day_remaining が 94 に更新される" "$cf" "seven_day_remaining=94"
assert_true "updated_at が更新される" \
    "$( ! grep -Fxq 'updated_at=1000000000' "$cf" && echo 1 || echo 0)" \
    "content: $(cat "$cf")"

# --- 条件8: キャッシュディレクトリが作成不可でも statusline 本体は壊れない
start_test "キャッシュディレクトリが書き込み不可でも exit 0 かつ標準出力は 2 行のまま"
new_cache_root
chmod 500 "$root"
run_statusline json_baseline XDG_CACHE_HOME="$root"
chmod 700 "$root"
assert_eq "書き込み不可でも exit 0" "0" "$run_rc"
assert_eq "書き込み不可でも statusline 本体は変わらない" \
    "$expected_line1
$expected_line2_baseline" "$run_out"
assert_eq "書き込み不可でも stderr は空" "" "$run_err"
assert_eq "標準出力は 2 行のまま" "2" "$(printf '%s\n' "$run_out" | wc -l | tr -d ' ')"

# --- 条件9: キャッシュ書き込みの副作用が標準出力・標準エラーを汚さない
start_test "キャッシュ書き込みの副作用が statusline の標準出力・標準エラーを汚さない"
new_cache_root
run_statusline json_baseline XDG_CACHE_HOME="$root"
assert_eq "キャッシュ書き込みの出力が混入しない" \
    "$expected_line1
$expected_line2_baseline" "$run_out"
assert_eq "標準出力は 2 行のまま" "2" "$(printf '%s\n' "$run_out" | wc -l | tr -d ' ')"
assert_eq "stderr は空" "" "$run_err"

# --- 条件10(退行): 既存の標準出力（2行）が変わらない
start_test "既存の statusline 標準出力が退行していない（正規形入力）"
new_cache_root
run_statusline json_baseline XDG_CACHE_HOME="$root"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "標準出力が従来どおり 2 行" \
    "$expected_line1
$expected_line2_baseline" "$run_out"
assert_eq "stderr は空" "" "$run_err"

start_test "既存の statusline 標準出力が退行していない（current_usage なし）"
new_cache_root
run_statusline json_no_usage XDG_CACHE_HOME="$root"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "新規セッションの標準出力" \
    "$expected_line1
⛁ 0.0k/200.0k (0.0%) │ ⚡ 5h:79% rem 7d:94% rem" "$run_out"

start_test "既存の statusline 標準出力が退行していない（context_window_size = 0）"
new_cache_root
run_statusline json_zero_context XDG_CACHE_HOME="$root"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "ゼロ除算ガード時の標準出力" \
    "$expected_line1
⛁ N/A │ ⚡ 5h:79% rem 7d:94% rem" "$run_out"

start_test "既存の statusline 標準出力が退行していない（rate_limits 欠落）"
new_cache_root
run_statusline json_no_rate_limits XDG_CACHE_HOME="$root"
assert_eq "rate_limits 欠落でも exit 0" "0" "$run_rc"
assert_eq "rate_limits 欠落時の statusline 本体" \
    "$expected_line1
⛁ 42.0k/200.0k (21.0%) │ ⚡ 5h:-- 7d:--" "$run_out"

# --- 条件11: 一時ファイルはキャッシュファイルと同一ディレクトリに作る
# 別ファイルシステムをまたぐと mv が rename(2) にならず、読み手（claude-rate-status）
# へ部分状態が見えうる。同一ディレクトリであることは原子性の前提条件なので、
# mv の呼び出し引数を記録するスパイで一時ファイルの置き場所を観測する。
start_test "キャッシュの一時ファイルがキャッシュファイルと同一ディレクトリに作られる"
spy_bin="$work/spybin"
mv_log="$work/mv-args.log"
real_mv="$(command -v mv)"
mkdir -p "$spy_bin"
for link in "$real_bin"/*; do
    name="$(basename "$link")"
    [ "$name" = "mv" ] && continue
    ln -sf "$(readlink "$link")" "$spy_bin/$name"
done
cat > "$spy_bin/mv" <<SPYEOF
#!/bin/sh
for a in "\$@"; do printf '%s\n' "\$a"; done >> "$mv_log"
exec "$real_mv" "\$@"
SPYEOF
chmod +x "$spy_bin/mv"

new_cache_root
: > "$mv_log"
run_statusline_with_path "$spy_bin" json_baseline XDG_CACHE_HOME="$root"
cf="$(cache_file "$root")"
tmp_arg="$(grep -F '.tmp.' "$mv_log" | head -1)"
assert_true "アトミック書き込みが一時ファイルを経由する" \
    "$([ -n "$tmp_arg" ] && echo 1 || echo 0)" \
    "mv args: $(tr '\n' ' ' < "$mv_log")"
assert_eq "一時ファイルの親ディレクトリがキャッシュファイルと同一" \
    "$(dirname "$cf")" "$(dirname "$tmp_arg")"
assert_true "移動先がキャッシュファイル本体" \
    "$(grep -Fxq -- "$cf" "$mv_log" && echo 1 || echo 0)" \
    "cache: $cf
mv args: $(tr '\n' ' ' < "$mv_log")"
assert_true "キャッシュファイルが生成される（スパイ経由）" \
    "$([ -f "$cf" ] && echo 1 || echo 0)" "not found: $cf"
assert_eq "スパイ経由でも exit 0" "0" "$run_rc"
assert_eq "スパイ経由でも stderr は空" "" "$run_err"
assert_eq "スパイ経由でも標準出力は 2 行" "2" "$(printf '%s\n' "$run_out" | wc -l | tr -d ' ')"

# --- 条件12: キャッシュパスがディレクトリでも一時ファイルが蓄積しない
# mv はターゲットがディレクトリだと「その中への移動」として成功するため、
# 失敗時のクリーンアップ分岐に入らず、実行のたびに一時ファイルが残りうる。
start_test "キャッシュパスがディレクトリでも一時ファイルを残さない"
new_cache_root
cf="$(cache_file "$root")"
mkdir -p "$cf"
for attempt in 1 2 3; do
    run_statusline json_baseline XDG_CACHE_HOME="$root"
    assert_eq "ディレクトリ衝突でも exit 0（$attempt 回目）" "0" "$run_rc"
    assert_eq "ディレクトリ衝突でも statusline 本体は変わらない（$attempt 回目）" \
        "$expected_line1
$expected_line2_baseline" "$run_out"
    assert_eq "ディレクトリ衝突でも標準出力は 2 行（$attempt 回目）" \
        "2" "$(printf '%s\n' "$run_out" | wc -l | tr -d ' ')"
    assert_eq "ディレクトリ衝突でも stderr は空（$attempt 回目）" "" "$run_err"
done
leftover="$(find "$root" -type f ! -name 'rate-limits' | sort)"
assert_eq "一時ファイルが 1 つも残らない" "" "$leftover"


# --- 条件13: mv 自体が失敗して stderr へ書いても statusline の出力を汚さない
# cache_path がディレクトリでないケース（= -d ガードを通過するケース）でも、
# mv は権限・ファイルシステムの都合で失敗し、その理由を stderr へ書く。
# statusLine の出力は Claude Code の画面そのものなので、キャッシュ書き込みの
# 失敗は診断メッセージも含めて完全に握り潰すこと。
# 実ファイルシステムでこの状態を作るには別ユーザー・別 FS が要るため、
# 失敗して stderr へ書く mv をスパイとして差し込み、契約として固定する。
start_test "mv が失敗して stderr へ書いても statusline の標準出力・標準エラーを汚さない"
failing_mv_bin="$work/failmvbin"
mkdir -p "$failing_mv_bin"
for link in "$real_bin"/*; do
    name="$(basename "$link")"
    [ "$name" = "mv" ] && continue
    ln -sf "$(readlink "$link")" "$failing_mv_bin/$name"
done
cat > "$failing_mv_bin/mv" <<'FAILMVEOF'
#!/bin/sh
printf 'mv: cannot move to target: simulated failure\n' >&2
exit 1
FAILMVEOF
chmod +x "$failing_mv_bin/mv"

new_cache_root
cf="$(cache_file "$root")"
run_statusline_with_path "$failing_mv_bin" json_baseline XDG_CACHE_HOME="$root"
assert_eq "mv 失敗でも exit 0" "0" "$run_rc"
assert_eq "mv 失敗でも statusline 本体は変わらない" \
    "$expected_line1
$expected_line2_baseline" "$run_out"
assert_eq "mv 失敗でも標準出力は 2 行" "2" "$(printf '%s\n' "$run_out" | wc -l | tr -d ' ')"
assert_eq "mv の診断メッセージが stderr へ漏れない" "" "$run_err"
assert_true "mv 失敗時はキャッシュファイルが作られない" \
    "$([ ! -e "$cf" ] && echo 1 || echo 0)" \
    "tree: $(find "$root" 2>/dev/null | sort | tr '\n' ' ')"
assert_eq "mv 失敗時も一時ファイルが残らない" "" \
    "$(find "$root" -type f | sort | tr '\n' ' ' | sed 's/ $//')"



# ---------------------------------------------------------------------------
printf '\n---------------------------------------------\n'
printf 'passed: %d  failed: %d\n' "$pass_count" "$fail_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0
