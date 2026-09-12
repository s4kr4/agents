#!/usr/bin/env bash
# claude-rate-status の振る舞いテスト
#
# 実行: scripts/test-claude-rate-status.sh
#
# claude-rate-status は herdr サーバー側（タブバーの command エントリ）から
# 定期実行される読み出し専用スクリプト。claude-statusline が書き出した
# レートリミットキャッシュを読み、1 行のサマリを標準出力へ出す。
#
# 対象スクリプトは env -i で起動し、PATH には本テストが用意した
# シンボリックリンクだけを含める。date は意図的に含めない
# （時刻取得は bash 組み込み printf '%(%s)T' を使う）。
# 書き込み系コマンド（mkdir / mv / mktemp / rm / touch）も含めないため、
# 副作用を伴う実装は PATH 解決の時点で失敗する。

set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$script_dir/claude-rate-status"
statusline_target="$script_dir/claude-statusline"
bash_bin="$(command -v bash)"

work="$(mktemp -d)"
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

rs_bin="$work/rsbin"       # claude-rate-status 用（読み出し専用）
sl_bin="$work/slbin"       # ラウンドトリップ用の claude-statusline 向け
dot_stub="$work/dotstub"
home_stub="$work/home"
proj_dir="$work/myproj"

mkdir -p "$rs_bin" "$sl_bin" "$dot_stub/etc/lib" "$home_stub" "$proj_dir"

# 読み出し専用スクリプトに許すコマンド。
for cmd in cat grep sed awk tr head wc; do
    path="$(command -v "$cmd" 2>/dev/null)"
    if [ -z "$path" ]; then
        printf 'FATAL: required command not found: %s\n' "$cmd" >&2
        exit 2
    fi
    ln -sf "$path" "$rs_bin/$cmd"
done

for forbidden in date mkdir mv mktemp rm touch; do
    if [ -e "$rs_bin/$forbidden" ]; then
        printf 'FATAL: %s must not be exposed to claude-rate-status\n' "$forbidden" >&2
        exit 2
    fi
done

# claude-statusline 用（test-claude-statusline.sh と同じ集合）
for cmd in cat jq basename git whoami hostname awk grep wc tr sed dirname mkdir mv mktemp rm; do
    path="$(command -v "$cmd" 2>/dev/null)"
    if [ -z "$path" ]; then
        printf 'FATAL: required command not found: %s\n' "$cmd" >&2
        exit 2
    fi
    ln -sf "$path" "$sl_bin/$cmd"
done

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
# 期待出力（バイト単位で比較する）
# ---------------------------------------------------------------------------

exp_fresh="$work/expected-fresh.out"
printf '5h:79%% 7d:94%%\n' > "$exp_fresh"

# stale は末尾に ASCII の '*' を付けて出し続ける。放置中は消費が進まず
# ローリングウィンドウから古い消費が外れるだけなので、古い残量は
# 過小評価（保守的な下限）であり出し続けても実害がない。
exp_stale="$work/expected-stale.out"
printf '5h:79%% 7d:94%%*\n' > "$exp_stale"

exp_edge="$work/expected-edge.out"
printf '5h:0%% 7d:100%%\n' > "$exp_edge"

exp_edge_stale="$work/expected-edge-stale.out"
printf '5h:0%% 7d:100%%*\n' > "$exp_edge_stale"

exp_zero="$work/expected-zero.out"
printf '5h:0%% 7d:0%%\n' > "$exp_zero"

exp_zero_stale="$work/expected-zero-stale.out"
printf '5h:0%% 7d:0%%*\n' > "$exp_zero_stale"

fullwidth_zero="$(printf '\xef\xbc\x90')"    # U+FF10 FULLWIDTH DIGIT ZERO
arabic_indic_five="$(printf '\xd9\xa5')"     # U+0665 ARABIC-INDIC DIGIT FIVE

# 十分に古いが数値としては完全に有効なエポック秒（2001-09-09）。
# 「古い」ことと「壊れている」ことを分けて固定するために使う。
old_epoch=1000000000

# ---------------------------------------------------------------------------
# 実行ヘルパー
# ---------------------------------------------------------------------------

out_file="$work/stdout.raw"
err_file="$work/stderr.raw"
run_out=""
run_err=""
run_rc=0

# run_rate_status_with_path <PATH> [KEY=VALUE ...]
run_rate_status_with_path() {
    local path_value="$1"
    shift
    : > "$out_file"
    : > "$err_file"
    env -i \
        PATH="$path_value" \
        HOME="$home_stub" \
        "$@" \
        "$bash_bin" "$target" > "$out_file" 2> "$err_file"
    run_rc=$?
    run_out="$(cat "$out_file")"
    run_err="$(cat "$err_file")"
}

# run_rate_status [KEY=VALUE ...]
run_rate_status() {
    run_rate_status_with_path "$rs_bin" "$@"
}

cache_root_seq=0
new_cache_root() {
    cache_root_seq=$((cache_root_seq + 1))
    root="$work/xdg$cache_root_seq"
    mkdir -p "$root/claude-statusline"
}

cache_file() {
    printf '%s/claude-statusline/rate-limits' "$1"
}

# assert_stdout_bytes <label> <expected-file>
assert_stdout_bytes() {
    local label="$1" expected_file="$2"
    if cmp -s "$out_file" "$expected_file"; then
        ok
    else
        fail "$label" "expected: $(od -c "$expected_file" | head -3 | tr '\n' ' ')
actual  : $(od -c "$out_file" | head -3 | tr '\n' ' ')"
    fi
}

# assert_stdout_empty <label>
assert_stdout_empty() {
    local label="$1"
    if [ ! -s "$out_file" ]; then
        ok
    else
        fail "$label" "stdout: $(od -c "$out_file" | head -3 | tr '\n' ' ')"
    fi
}

# run_with_age <cache-root> <age-seconds>
# 実行中に秒が跨ぐと期待する経過秒がずれるため、実行前後の epoch 秒が
# 一致するまで最大 5 回やり直す。
run_with_age() {
    local root="$1" age="$2"
    local attempt=0 t0 t1
    while :; do
        printf -v t0 '%(%s)T' -1
        printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=%s\n' \
            "$((t0 - age))" > "$(cache_file "$root")"
        run_rate_status XDG_CACHE_HOME="$root"
        printf -v t1 '%(%s)T' -1
        [ "$t0" = "$t1" ] && return 0
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 5 ]; then
            return 1
        fi
    done
}

# run_corrupt <label> <content-producer>
# 破損キャッシュを置いて実行し、stdout 空 / exit 0 / stderr 空 を確認する
run_corrupt() {
    local label="$1" producer="$2"
    local root
    new_cache_root
    "$producer" > "$(cache_file "$root")"
    run_rate_status XDG_CACHE_HOME="$root"
    assert_stdout_empty "$label: 標準出力が空"
    assert_eq "$label: exit 0" "0" "$run_rc"
    assert_eq "$label: stderr が空" "" "$run_err"
}

# write_cache <five_hour> <seven_day> <updated_at>
# 新しいキャッシュルートを作り、3 キーのキャッシュを書く
write_cache() {
    new_cache_root
    printf 'five_hour_remaining=%s\nseven_day_remaining=%s\nupdated_at=%s\n' \
        "$1" "$2" "$3" > "$(cache_file "$root")"
}

# run_cache_case <label> <five_hour> <seven_day> <updated_at> <expected-file>
# 出力されることを期待するケース。バイト単位で比較する。
run_cache_case() {
    local label="$1" expected="$5"
    write_cache "$2" "$3" "$4"
    run_rate_status XDG_CACHE_HOME="$root"
    assert_stdout_bytes "$label: 標準出力が期待バイト列と一致" "$expected"
    assert_eq "$label: exit 0" "0" "$run_rc"
    assert_eq "$label: stderr が空" "" "$run_err"
}

# run_no_output_case <label> <five_hour> <seven_day> <updated_at>
# 無出力を期待するケース。
run_no_output_case() {
    local label="$1"
    write_cache "$2" "$3" "$4"
    run_rate_status XDG_CACHE_HOME="$root"
    assert_stdout_empty "$label: 標準出力が空"
    assert_eq "$label: exit 0" "0" "$run_rc"
    assert_eq "$label: stderr が空" "" "$run_err"
}

# ---------------------------------------------------------------------------
# 入力 JSON（ラウンドトリップ用）
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------

# --- 条件1: fresh なキャッシュから 1 行のサマリを出力する
start_test "fresh なキャッシュから 5h:79% 7d:94% を 1 行で出力する"
new_cache_root
run_with_age "$root" 0
assert_stdout_bytes "標準出力が期待バイト列（末尾改行 1 個）と一致" "$exp_fresh"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"
assert_eq "標準出力は 1 行" "1" "$(wc -l < "$out_file" | tr -d ' ')"

# --- 条件2: 鮮度の境界（now - updated_at > 900 が stale）
# 経過秒がずれると境界ケースの意味が失われるため、run_with_age の
# リトライが尽きていないことも毎回確認する。
start_test "899 秒前のキャッシュは fresh として出力する"
new_cache_root
run_with_age "$root" 899; wa_rc=$?
assert_eq "経過秒 899 のキャッシュを用意できた" "0" "$wa_rc"
assert_stdout_bytes "899 秒前は * なしで出力される" "$exp_fresh"
assert_eq "899 秒前でも exit 0" "0" "$run_rc"
assert_eq "899 秒前でも stderr は空" "" "$run_err"

start_test "ちょうど 900 秒前のキャッシュは fresh として出力する（境界は inclusive）"
new_cache_root
run_with_age "$root" 900; wa_rc=$?
assert_eq "経過秒 900 のキャッシュを用意できた" "0" "$wa_rc"
assert_stdout_bytes "900 秒前は * なしで出力される" "$exp_fresh"
assert_eq "900 秒前でも exit 0" "0" "$run_rc"
assert_eq "900 秒前でも stderr は空" "" "$run_err"

start_test "901 秒前のキャッシュは stale として末尾に * を付けて出力する"
new_cache_root
run_with_age "$root" 901; wa_rc=$?
assert_eq "経過秒 901 のキャッシュを用意できた" "0" "$wa_rc"
assert_stdout_bytes "901 秒前は * 付きで出力される" "$exp_stale"
assert_eq "901 秒前でも exit 0" "0" "$run_rc"
assert_eq "901 秒前でも stderr は空" "" "$run_err"
assert_eq "stale でも標準出力は 1 行" "1" "$(wc -l < "$out_file" | tr -d ' ')"

start_test "1 日前のキャッシュは stale として末尾に * を付けて出力する"
new_cache_root
run_with_age "$root" 86400; wa_rc=$?
assert_eq "経過秒 86400 のキャッシュを用意できた" "0" "$wa_rc"
assert_stdout_bytes "1 日前は * 付きで出力される" "$exp_stale"
assert_eq "1 日前でも exit 0" "0" "$run_rc"
assert_eq "1 日前でも stderr は空" "" "$run_err"

# 放置中は残量が回復するだけなので、古い値は過小評価にしかならない。
# 何日古くても出力を打ち切る上限は設けない。
start_test "1 年前のキャッシュでも stale として出力し続ける（上限なし）"
new_cache_root
run_with_age "$root" 31536000; wa_rc=$?
assert_eq "経過秒 31536000 のキャッシュを用意できた" "0" "$wa_rc"
assert_stdout_bytes "1 年前でも * 付きで出力される" "$exp_stale"
assert_eq "1 年前でも exit 0" "0" "$run_rc"
assert_eq "1 年前でも stderr は空" "" "$run_err"

# --- 条件3: 未来の updated_at（クロックスキュー）は fresh 扱い
start_test "未来の updated_at（クロックスキュー）は fresh として扱う"
new_cache_root
run_with_age "$root" -3600; wa_rc=$?
assert_eq "1 時間先のキャッシュを用意できた" "0" "$wa_rc"
assert_stdout_bytes "1 時間先の updated_at は * なしで出力される" "$exp_fresh"
assert_eq "未来の updated_at でも exit 0" "0" "$run_rc"
assert_eq "未来の updated_at でも stderr は空" "" "$run_err"

start_test "桁数上限ぎりぎり（18 桁）の未来 updated_at も fresh として扱う"
run_cache_case "updated_at=999999999999999999" 79 94 999999999999999999 "$exp_fresh"

# --- 条件4: 値の境界（0 と 100 は有効）
start_test "残量 0% / 100% は fresh なら * なしで出力する"
printf -v now '%(%s)T' -1
run_cache_case "fresh・0 と 100" 0 100 "$now" "$exp_edge"

start_test "残量 0% / 100% は stale なら * 付きで出力する"
run_cache_case "stale・0 と 100" 0 100 "$old_epoch" "$exp_edge_stale"

start_test "残量が両方 0 でも stale なら * 付きで出力する"
run_cache_case "stale・0 と 0" 0 0 "$old_epoch" "$exp_zero_stale"

# --- 条件5: キャッシュ不在
start_test "キャッシュファイルが存在しない場合は何も出力しない"
new_cache_root
run_rate_status XDG_CACHE_HOME="$root"
assert_stdout_empty "ファイル不在で標準出力が空"
assert_eq "ファイル不在でも exit 0" "0" "$run_rc"
assert_eq "ファイル不在でも stderr は空" "" "$run_err"

start_test "キャッシュディレクトリごと存在しない場合は何も出力しない"
root="$work/xdg-missing"
rm -rf "$root"
run_rate_status XDG_CACHE_HOME="$root"
assert_stdout_empty "ディレクトリ不在で標準出力が空"
assert_eq "ディレクトリ不在でも exit 0" "0" "$run_rc"
assert_eq "ディレクトリ不在でも stderr は空" "" "$run_err"

# --- 条件6: 破損・非正規形のキャッシュ
c_empty() { : ; }
run_corrupt "空ファイル" c_empty

c_newlines() { printf '\n\n\n'; }
run_corrupt "改行のみ" c_newlines

c_binary() { printf '\x00\x01\x02\xff\xfe\x7f\x00binary garbage\x00\x01'; }
run_corrupt "バイナリ" c_binary

c_missing_five() { printf 'seven_day_remaining=94\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "five_hour_remaining 欠落" c_missing_five

c_missing_seven() { printf 'five_hour_remaining=79\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "seven_day_remaining 欠落" c_missing_seven

c_missing_updated() { printf 'five_hour_remaining=79\nseven_day_remaining=94\n'; }
run_corrupt "updated_at 欠落" c_missing_updated

c_non_numeric() { printf 'five_hour_remaining=abc\nseven_day_remaining=94\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "非数値の残量" c_non_numeric

c_negative() { printf 'five_hour_remaining=-1\nseven_day_remaining=94\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "負値の残量" c_negative

c_over_100() { printf 'five_hour_remaining=101\nseven_day_remaining=94\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "five_hour が 101 の残量" c_over_100

# 残量の 2 つのガードは対称。seven_day 側だけ保護が無いと、
# 上限チェックを片方だけ落としても気付けない。
c_seven_over_100() { printf 'five_hour_remaining=79\nseven_day_remaining=101\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "seven_day が 101 の残量" c_seven_over_100

c_seven_non_numeric() { printf 'five_hour_remaining=79\nseven_day_remaining=abc\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "seven_day が非数値の残量" c_seven_non_numeric

c_seven_negative() { printf 'five_hour_remaining=79\nseven_day_remaining=-1\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "seven_day が負値の残量" c_seven_negative

c_float() { printf 'five_hour_remaining=79.5\nseven_day_remaining=94\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "小数の残量" c_float

c_pct_suffix() { printf 'five_hour_remaining=79%%\nseven_day_remaining=94\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "% 付きの残量" c_pct_suffix

c_updated_non_numeric() { printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=not-a-number\n'; }
run_corrupt "非数値の updated_at" c_updated_non_numeric

c_updated_empty() { printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=\n'; }
run_corrupt "空の updated_at" c_updated_empty

# 桁数上限（18 桁）を超える updated_at は算術評価が破綻しうるため受理しない
c_updated_19_digits() { printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=1234567890123456789\n'; }
run_corrupt "19 桁の updated_at" c_updated_19_digits

c_long_line() {
    local pad
    pad="$(head -c 100000 /dev/zero | tr '\0' '9')"
    printf 'five_hour_remaining=%s\nseven_day_remaining=94\nupdated_at=%s\n' \
        "$pad" "$(printf '%(%s)T' -1)"
}
run_corrupt "超長行の残量" c_long_line

# 手編集された CRLF ファイル: 値が ^[0-9]+$ を満たさないため「データなし」扱い
c_crlf() { printf 'five_hour_remaining=79\r\nseven_day_remaining=94\r\nupdated_at=%s\r\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "CRLF 改行" c_crlf

c_trailing_space() { printf 'five_hour_remaining=79 \nseven_day_remaining=94\nupdated_at=%s\n' "$(printf '%(%s)T' -1)"; }
run_corrupt "値の末尾に空白" c_trailing_space

# --- 条件7: 未知のキーが混ざっていても正しく読める
start_test "未知のキーが混ざっていても正しく読める"
new_cache_root
printf -v now '%(%s)T' -1
printf 'schema=2\nfive_hour_remaining=79\nnote=hello=world\nseven_day_remaining=94\nupdated_at=%s\n' \
    "$now" > "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_stdout_bytes "未知キーがあっても期待どおり出力する" "$exp_fresh"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "未知キーがあっても stderr は空" "" "$run_err"

# --- 条件8: インジェクション（source / eval 実装の検出器）
start_test "キャッシュ内のコマンド置換が実行されない（\$( ) 形式）"
new_cache_root
pwned="$work/pwned-dollar"
rm -f "$pwned"
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=$(printf pwned > "%s")\nseven_day_remaining=94\nupdated_at=%s\n' \
    "$pwned" "$now" > "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_true "\$( ) が実行されていない" "$([ ! -e "$pwned" ] && echo 1 || echo 0)" \
    "created: $pwned"
assert_stdout_empty "不正値なので標準出力は空"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "\$( ) 混入でも stderr は空" "" "$run_err"

start_test "キャッシュ内のコマンド置換が実行されない（バッククォート形式）"
new_cache_root
pwned="$work/pwned-backtick"
rm -f "$pwned"
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=`printf pwned > "%s"`\nseven_day_remaining=94\nupdated_at=%s\n' \
    "$pwned" "$now" > "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_true "バッククォートが実行されていない" "$([ ! -e "$pwned" ] && echo 1 || echo 0)" \
    "created: $pwned"
assert_stdout_empty "不正値なので標準出力は空"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "バッククォート混入でも stderr は空" "" "$run_err"

start_test "キャッシュ内のセミコロン区切りコマンドが実行されない"
new_cache_root
pwned="$work/pwned-semicolon"
rm -f "$pwned"
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=79; printf pwned > "%s"\nseven_day_remaining=94\nupdated_at=%s\n' \
    "$pwned" "$now" > "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_true "セミコロン以降が実行されていない" "$([ ! -e "$pwned" ] && echo 1 || echo 0)" \
    "created: $pwned"
assert_stdout_empty "不正値なので標準出力は空"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "セミコロン混入でも stderr は空" "" "$run_err"

# --- 条件9: 副作用ゼロ
# 書き込み系コマンド（mkdir / touch / mv / rm）が使える PATH で実行する。
# curated PATH ではそれらが解決できず副作用が「たまたま」起きないため、
# 実運用（herdr サーバー）に近い PATH で振る舞いとして固定する。
start_test "キャッシュが存在しない場合でもディレクトリ・ファイルを作らない"
root="$work/xdg-nosideeffect"
rm -rf "$root"
run_rate_status_with_path "/usr/bin:/bin" XDG_CACHE_HOME="$root"
assert_true "キャッシュルートが作られない" "$([ ! -e "$root" ] && echo 1 || echo 0)" \
    "tree: $(find "$root" 2>/dev/null | sort | tr '\n' ' ')"
assert_eq "副作用チェック時も exit 0" "0" "$run_rc"
assert_eq "副作用チェック時も stderr は空" "" "$run_err"

start_test "読み出しがキャッシュディレクトリを変更しない"
new_cache_root
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=%s\n' "$now" > "$(cache_file "$root")"
before_tree="$(find "$root" | sort)"
before_content="$(cat "$(cache_file "$root")")"
run_rate_status_with_path "/usr/bin:/bin" XDG_CACHE_HOME="$root"
assert_eq "ディレクトリ構成が変化しない" "$before_tree" "$(find "$root" | sort)"
assert_eq "キャッシュ内容が変化しない" "$before_content" "$(cat "$(cache_file "$root")")"
assert_eq "読み出し時も stderr は空" "" "$run_err"

start_test "読み出しが \$HOME 配下を変更しない"
home_before="$(find "$home_stub" | sort)"
new_cache_root
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=%s\n' "$now" > "$(cache_file "$root")"
run_rate_status_with_path "/usr/bin:/bin" XDG_CACHE_HOME="$root"
assert_eq "\$HOME 配下が変化しない" "$home_before" "$(find "$home_stub" | sort)"
assert_eq "\$HOME 非変更チェック時も stderr は空" "" "$run_err"

# --- 条件10: XDG_CACHE_HOME の 2 段解決
start_test "XDG_CACHE_HOME 未設定時は \$HOME/.cache へフォールバックする"
rm -rf "$home_stub/.cache"
mkdir -p "$home_stub/.cache/claude-statusline"
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=%s\n' \
    "$now" > "$home_stub/.cache/claude-statusline/rate-limits"
run_rate_status
assert_stdout_bytes "\$HOME/.cache から読める" "$exp_fresh"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "stderr は空" "" "$run_err"

start_test "XDG_CACHE_HOME にキャッシュが無ければ \$HOME/.cache へフォールバックする"
new_cache_root
rm -f "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_stdout_bytes "XDG 側が空でも \$HOME/.cache から読める" "$exp_fresh"
assert_eq "exit 0" "0" "$run_rc"
assert_eq "フォールバック時も stderr は空" "" "$run_err"

start_test "XDG_CACHE_HOME にキャッシュがあればそちらを優先する"
new_cache_root
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=0\nseven_day_remaining=100\nupdated_at=%s\n' "$now" > "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_stdout_bytes "XDG 側の値が使われる" "$exp_edge"
assert_eq "XDG 優先時も exit 0" "0" "$run_rc"
assert_eq "XDG 優先時も stderr は空" "" "$run_err"

rm -rf "$home_stub/.cache"

# --- 条件11: 最小 PATH で動作する
start_test "最小 PATH（/usr/bin:/bin）でも正常に動作する"
new_cache_root
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=%s\n' "$now" > "$(cache_file "$root")"
run_rate_status_with_path "/usr/bin:/bin" XDG_CACHE_HOME="$root"
assert_stdout_bytes "最小 PATH でも期待どおり出力する" "$exp_fresh"
assert_eq "最小 PATH でも exit 0" "0" "$run_rc"
assert_eq "最小 PATH でも stderr は空" "" "$run_err"

# --- 条件12: ラウンドトリップ（claude-statusline -> claude-rate-status）
start_test "claude-statusline が書いたキャッシュを claude-rate-status が読める（ラウンドトリップ）"
rt_root="$work/xdg-roundtrip"
rm -rf "$rt_root"
mkdir -p "$rt_root"
json_baseline > "$work/roundtrip-input.json"
rt_sl_err="$work/roundtrip-sl-err.txt"
rt_sl_out="$(env -i \
    PATH="$sl_bin" \
    HOME="$home_stub" \
    DOTPATH="$dot_stub" \
    XDG_CACHE_HOME="$rt_root" \
    "$bash_bin" "$statusline_target" < "$work/roundtrip-input.json" 2>"$rt_sl_err")"
rt_sl_rc=$?
assert_eq "claude-statusline が exit 0" "0" "$rt_sl_rc"
assert_eq "claude-statusline の stderr は空" "" "$(cat "$rt_sl_err")"
assert_eq "claude-statusline の標準出力は 2 行" "2" "$(printf '%s\n' "$rt_sl_out" | wc -l | tr -d ' ')"
assert_true "claude-statusline がキャッシュを書いた" \
    "$([ -f "$rt_root/claude-statusline/rate-limits" ] && echo 1 || echo 0)" \
    "tree: $(find "$rt_root" 2>/dev/null | sort | tr '\n' ' ')"

run_rate_status XDG_CACHE_HOME="$rt_root"
assert_stdout_bytes "claude-rate-status が同じ契約で読み出せる" "$exp_fresh"
assert_eq "ラウンドトリップで exit 0" "0" "$run_rc"
assert_eq "ラウンドトリップで stderr は空" "" "$run_err"
assert_match "5h 残 79 を含む" '5h[^0-9]*79' "$run_out"
assert_match "7d 残 94 を含む" '7d[^0-9]*94' "$run_out"

# --- 条件13: 先頭ゼロの数値は「データなし」として扱う
# 値が ^[0-9]+$ を満たしていても、算術評価 $(( )) は先頭ゼロを 8 進数として
# 解釈する。test（[ ]）は基数 10 固定なので、同じ値でも評価する構文によって
# 挙動が変わる。8 進として不正な桁（8 / 9）を含む場合は算術エラーとなり、
# bash はエラーが起きたコマンドリストを丸ごと破棄して次行から実行を続ける。
# そのため `[ ... ] || exit 0` 形式の鮮度ガードは発火せず、古い値が
# 出力され続けうる。先頭ゼロは受理せず「データなし」に倒すこと。

# run_leading_zero <key> <value>
# 指定キーだけを非正規形の値に差し替えた fresh キャッシュを置いて実行し、
# stdout 空 / exit 0 / stderr 空 を確認する。
run_leading_zero() {
    local key="$1" value="$2"
    local five=79 seven=94 upd root
    printf -v upd '%(%s)T' -1
    case "$key" in
        five_hour_remaining) five="$value" ;;
        seven_day_remaining) seven="$value" ;;
        updated_at) upd="$value" ;;
    esac
    new_cache_root
    printf 'five_hour_remaining=%s\nseven_day_remaining=%s\nupdated_at=%s\n' \
        "$five" "$seven" "$upd" > "$(cache_file "$root")"
    run_rate_status XDG_CACHE_HOME="$root"
    assert_stdout_empty "$key=$value: 標準出力が空"
    assert_eq "$key=$value: exit 0" "0" "$run_rc"
    assert_eq "$key=$value: stderr が空" "" "$run_err"
}

start_test "先頭ゼロの updated_at は鮮度ガードを素通りさせない"
run_leading_zero updated_at 08
run_leading_zero updated_at 09
run_leading_zero updated_at 00
run_leading_zero updated_at 007
run_leading_zero updated_at 0100
run_leading_zero updated_at 01788951688

start_test "先頭ゼロを付けた現在エポックの updated_at でも出力しない"
printf -v now '%(%s)T' -1
run_leading_zero updated_at "0$now"

start_test "先頭ゼロの five_hour_remaining は出力しない"
run_leading_zero five_hour_remaining 08
run_leading_zero five_hour_remaining 09
run_leading_zero five_hour_remaining 00
run_leading_zero five_hour_remaining 007
run_leading_zero five_hour_remaining 0100

start_test "先頭ゼロの seven_day_remaining は出力しない"
run_leading_zero seven_day_remaining 08
run_leading_zero seven_day_remaining 09
run_leading_zero seven_day_remaining 00
run_leading_zero seven_day_remaining 007
run_leading_zero seven_day_remaining 0100

start_test "先頭ゼロを含む両方の残量でも出力しない"
new_cache_root
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=08\nseven_day_remaining=08\nupdated_at=%s\n' "$now" > "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_stdout_empty "両方先頭ゼロで標準出力が空"
assert_eq "両方先頭ゼロでも exit 0" "0" "$run_rc"
assert_eq "両方先頭ゼロでも stderr は空" "" "$run_err"

# --- 条件14: 単独の 0 は先頭ゼロではないため受理し続ける（退行防止）
start_test "残量が両方 0 の fresh キャッシュは 5h:0% 7d:0% を出力する"
new_cache_root
printf -v now '%(%s)T' -1
printf 'five_hour_remaining=0\nseven_day_remaining=0\nupdated_at=%s\n' "$now" > "$(cache_file "$root")"
run_rate_status XDG_CACHE_HOME="$root"
assert_stdout_bytes "0 が 0 のまま出力される" "$exp_zero"
assert_eq "残量 0 でも exit 0" "0" "$run_rc"
assert_eq "残量 0 でも stderr は空" "" "$run_err"

# updated_at=0 は epoch 0（1970 年）を指す有効な数値。極端に古いだけなので
# 「壊れた値」ではなく stale として * 付きで出力する。
start_test "updated_at=0（epoch 0）は有効な数値として stale 出力する"
run_cache_case "updated_at=0" 79 94 0 "$exp_stale"

# --- 条件15: 数値の判定がロケールの照合順序に左右されない
# case パターンの [!0-9] は LC_COLLATE の照合順序で範囲が解決されるため、
# ロケール次第で全角数字（U+FF10）や Arabic-Indic 数字（U+0665）が
# 「0-9 の範囲内」と判定される。受理された非 ASCII 数字は後段の $(( )) や
# test -le で算術エラーになり、bash はエラーが起きたコマンドリストを丸ごと
# 破棄して次行から実行を続ける。そのため `[ ... ] || exit 0` 形式の鮮度ガード・
# 上限ガードが発火せず、古い値が stderr 汚染つきで出力されうる。
# 数値の判定は常に ASCII の 0-9 だけを受理すること。

# 対象スクリプトは env -i で起動するため、ロケールを明示的に渡さないと
# 実行時ロケールは C になり、このセクション全体が無条件 pass する空振りに
# なる。ロケール名の存在確認だけでは照合順序が実際に緩いことを保証できない
# ため、候補ロケールで [!0-9] が全角ゼロを取りこぼすかを実測して選ぶ。
hazard_locale=""
for candidate in en_US.UTF-8 en_US.utf8 C.UTF-8 ja_JP.UTF-8; do
    if env -i LC_ALL="$candidate" "$bash_bin" -c \
        'case "$1" in *[!0-9]*) exit 1 ;; esac; exit 0' _ "$fullwidth_zero" 2>/dev/null
    then
        hazard_locale="$candidate"
        break
    fi
done

# run_locale_case <label> <five_hour> <seven_day> <updated_at>
# $hazard_locale を明示的に渡して実行し、stdout 空 / exit 0 / stderr 空 を確認する
run_locale_case() {
    local label="$1" five="$2" seven="$3" upd="$4"
    new_cache_root
    printf 'five_hour_remaining=%s\nseven_day_remaining=%s\nupdated_at=%s\n' \
        "$five" "$seven" "$upd" > "$(cache_file "$root")"
    run_rate_status LC_ALL="$hazard_locale" XDG_CACHE_HOME="$root"
    assert_stdout_empty "$label: 標準出力が空"
    assert_eq "$label: exit 0" "0" "$run_rc"
    assert_eq "$label: stderr が空" "" "$run_err"
}

if [ -z "$hazard_locale" ]; then
    start_test "非 ASCII 数字を含むキャッシュは出力しない"
    skip "非 ASCII 数字を [0-9] の範囲に含めるロケールがこの環境に無いため、条件15 のケースを実行できなかった（locale -a に en_US.UTF-8 等が必要）"
else
    start_test "非 ASCII 数字で始まる updated_at は鮮度ガードを素通りさせない（LC_ALL=$hazard_locale）"
    printf -v now '%(%s)T' -1
    run_locale_case "updated_at=全角ゼロ+現在エポック" 79 94 "${fullwidth_zero}${now}"
    run_locale_case "updated_at=全角ゼロ単独" 79 94 "$fullwidth_zero"
    run_locale_case "updated_at=Arabic-Indic 数字" 79 94 "$arabic_indic_five"

    start_test "非 ASCII 数字の five_hour_remaining は出力しない（LC_ALL=$hazard_locale）"
    printf -v now '%(%s)T' -1
    run_locale_case "five_hour_remaining=全角ゼロ+79" "${fullwidth_zero}79" 94 "$now"
    run_locale_case "five_hour_remaining=Arabic-Indic 数字" "$arabic_indic_five" 94 "$now"

    start_test "非 ASCII 数字の seven_day_remaining は出力しない（LC_ALL=$hazard_locale）"
    printf -v now '%(%s)T' -1
    run_locale_case "seven_day_remaining=全角ゼロ+94" 79 "${fullwidth_zero}94" "$now"
    run_locale_case "seven_day_remaining=Arabic-Indic 数字" 79 "$arabic_indic_five" "$now"

    start_test "LC_ALL=$hazard_locale でも正規形キャッシュは従来どおり出力する"
    new_cache_root
    printf -v now '%(%s)T' -1
    printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=%s\n' \
        "$now" > "$(cache_file "$root")"
    run_rate_status LC_ALL="$hazard_locale" XDG_CACHE_HOME="$root"
    assert_stdout_bytes "非 C ロケールでも期待バイト列と一致" "$exp_fresh"
    assert_eq "非 C ロケールでも exit 0" "0" "$run_rc"
    assert_eq "非 C ロケールでも stderr は空" "" "$run_err"

    start_test "LC_ALL=$hazard_locale でも境界値 0 / 100 は出力する"
    new_cache_root
    printf -v now '%(%s)T' -1
    printf 'five_hour_remaining=0\nseven_day_remaining=100\nupdated_at=%s\n' \
        "$now" > "$(cache_file "$root")"
    run_rate_status LC_ALL="$hazard_locale" XDG_CACHE_HOME="$root"
    assert_stdout_bytes "非 C ロケールでも 0 と 100 が出力される" "$exp_edge"
    assert_eq "非 C ロケールの境界値でも exit 0" "0" "$run_rc"
    assert_eq "非 C ロケールの境界値でも stderr は空" "" "$run_err"

    start_test "LC_ALL=$hazard_locale でも stale キャッシュは * 付きで出力する"
    new_cache_root
    printf 'five_hour_remaining=79\nseven_day_remaining=94\nupdated_at=%s\n' \
        "$old_epoch" > "$(cache_file "$root")"
    run_rate_status LC_ALL="$hazard_locale" XDG_CACHE_HOME="$root"
    assert_stdout_bytes "非 C ロケールでも stale は * 付きで一致" "$exp_stale"
    assert_eq "非 C ロケールの stale でも exit 0" "0" "$run_rc"
    assert_eq "非 C ロケールの stale でも stderr は空" "" "$run_err"

    start_test "LC_ALL=$hazard_locale でも ASCII の先頭ゼロは出力しない"
    printf -v now '%(%s)T' -1
    run_locale_case "updated_at=0+現在エポック" 79 94 "0$now"
    run_locale_case "five_hour_remaining=08" 08 94 "$now"
    run_locale_case "seven_day_remaining=08" 79 08 "$now"
fi

# --- 条件16: 「古い値」と「壊れた値」を混同しない
# stale は「いつの値かは分かるが古い」状態なので、過小評価と分かる印（*）を
# 付けて出し続ける。一方で updated_at が数値として不正な場合は「いつの値か
# 判断できない」ため、古いのか新しいのかも決められず出力しない。
# 両者を同じ「出力しない」に倒すと前者の情報が失われ、両者を同じ「* 付きで
# 出力する」に倒すと素性不明の値を残量として提示することになる。

start_test "有効な数値で古い updated_at は stale として * 付きで出力する"
run_cache_case "updated_at=$old_epoch（2001 年）" 79 94 "$old_epoch" "$exp_stale"
run_cache_case "updated_at=1（1970 年）" 79 94 1 "$exp_stale"
run_cache_case "updated_at=0（epoch 0）" 79 94 0 "$exp_stale"

start_test "古そうに見えても数値として不正な updated_at は出力しない"
run_no_output_case "updated_at=${fullwidth_zero}${old_epoch}（全角ゼロ始まり）" \
    79 94 "${fullwidth_zero}${old_epoch}"
run_no_output_case "updated_at=0$old_epoch（先頭ゼロ）" 79 94 "0$old_epoch"
run_no_output_case "updated_at=08（先頭ゼロ）" 79 94 08
run_no_output_case "updated_at=${old_epoch}abc（数値以外を含む）" 79 94 "${old_epoch}abc"
run_no_output_case "updated_at=${arabic_indic_five}（Arabic-Indic 数字）" 79 94 "$arabic_indic_five"

start_test "残量が不正なら stale であっても出力しない"
run_no_output_case "stale + 非数値の five_hour_remaining" abc 94 "$old_epoch"
run_no_output_case "stale + 負値の five_hour_remaining" -1 94 "$old_epoch"
run_no_output_case "stale + 101 の seven_day_remaining" 79 101 "$old_epoch"
run_no_output_case "stale + 先頭ゼロの seven_day_remaining" 79 08 "$old_epoch"
run_no_output_case "stale + 小数の five_hour_remaining" 79.5 94 "$old_epoch"

start_test "stale なキャッシュを読んでも副作用を起こさない"
write_cache 79 94 "$old_epoch"
before_tree="$(find "$root" | sort)"
before_content="$(cat "$(cache_file "$root")")"
run_rate_status_with_path "/usr/bin:/bin" XDG_CACHE_HOME="$root"
assert_stdout_bytes "書き込み可能な PATH でも stale 出力は同じ" "$exp_stale"
assert_eq "stale 読み出しでディレクトリ構成が変化しない" "$before_tree" "$(find "$root" | sort)"
assert_eq "stale 読み出しでキャッシュ内容が変化しない" "$before_content" "$(cat "$(cache_file "$root")")"
assert_eq "stale 読み出し時も stderr は空" "" "$run_err"

# ---------------------------------------------------------------------------
printf '\n---------------------------------------------\n'
printf 'passed: %d  failed: %d  skipped: %d\n' "$pass_count" "$fail_count" "$skip_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0
