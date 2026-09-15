#!/usr/bin/env bash
# Claude Code の SessionStart フック: 共有メモリ（Vault, scope=global,
# tag=philosophy）のユーザー方針を additionalContext として注入する。
#
# 契約: セッション開始をブロックしない・stderr に何も出さない。取得に
# 失敗した場合は例外を投げず、必ず注意文の JSON へフォールバックする。
# そのため `set -e` は使わず、失敗経路をすべて明示的に処理し exit 0 で終える。
set -uo pipefail

# bash 自身の正規表現・文字クラス判定をロケールに依存させないための固定。
# ロケールによっては文字クラスが照合順序で解決され、例えば全角数字を
# LLM_MEMORY_HOOK_TIMEOUT の桁として誤って受理しうる。summary の文字数
# 計算・切り詰め・並べ替えは jq 側で行うためロケール非依存。
export LC_ALL=C

# ペイロード（session_id/cwd/transcript_path 等）は使わないが、呼び出し元の
# 書き込みをブロックしないために読み捨てる。
cat >/dev/null

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
memory_cli="${script_dir}/memory.py"
run_python="${script_dir}/run-python.sh"

heading="## ユーザーの作業方針（共有メモリ philosophy、自動注入）"
instruction="設計判断ではこの方針に照らして判断し、該当する項目を根拠として示すこと。プロジェクト固有の規約（AGENTS.md / CLAUDE.md 等）と衝突する場合はプロジェクト規約を優先する。"
# emit_notice に直接埋め込むため事前にエスケープ済み。この文字列に含まれる
# JSON 上の特殊文字は "philosophy" を囲む二重引用符だけ。
notice_json_escaped='共有メモリから作業方針（philosophy）を自動で読み込めませんでした。設計判断の前に shared-memory の search（scope=global, tags=[\"philosophy\"]）で取得してください。'

# LLM_MEMORY_HOOK_TIMEOUT は 1〜8 の整数のみ有効。settings.json 側のフック
# タイムアウト（10秒）を超えないための上限で、それ以外の値（0・負数・9以上・
# 小数・空・非数字・全角数字など）は既定の 5 秒を使う。
timeout_value="${LLM_MEMORY_HOOK_TIMEOUT:-5}"
if ! [[ "$timeout_value" =~ ^[1-8]$ ]]; then
  timeout_value=5
fi

# jq を一切使わない: jq が PATH に無くても有効な JSON を返せる必要がある。
# 文言は静的であらかじめエスケープ済みなので実行時のエスケープは不要。
# %s（%b ではない）でバックスラッシュを再解釈させない。
emit_notice() {
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}' \
    "$notice_json_escaped"
  exit 0
}

# command -v は jq を起動しない（存在確認だけ）ので、CLI 側のタイムアウト
# 予算を消費しない。
command -v jq >/dev/null 2>&1 || emit_notice

# macOS の system bash には GNU timeout が無いことがあるため gtimeout に
# フォールバックする。どちらも無ければ CLI を実行せずに注意文を返す。
if command -v timeout >/dev/null 2>&1; then
  timeout_cmd="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  timeout_cmd="gtimeout"
else
  emit_notice
fi

# mktemp 自身の失敗メッセージ（TMPDIR が無い・書き込めない・通常ファイル等）
# が stderr に漏れないよう抑止する。
tmp_dir="$(mktemp -d 2>/dev/null)" || emit_notice
trap 'rm -rf "$tmp_dir"' EXIT
stdout_file="${tmp_dir}/stdout"
stderr_file="${tmp_dir}/stderr"
output_file="${tmp_dir}/output"
body_filter_file="${tmp_dir}/body.jq"

# CLI の応答を検証し本文を組み立てる jq プログラム。ファイルに書き出して
# `-f` で渡す（この書き出し自体は CLI の出力を読まないため、共有タイムア
# ウトの外で行ってよい）。
cat >"$body_filter_file" <<'JQ_FILTER'
def base_key: split("/") | last;
# write-memory が未知のキーで書いた記憶は summarize_memory() の既定である
# "<key>: <value>" 形式になる（memory.py 参照）ため、ここで一つだけ取り除く。
# --key はハイフン・アンダースコアどちらでも入力されうるので両方試す。
def strip_prefix($k; $s):
  ($k + ": ") as $h
  | (($k | gsub("-"; "_")) + ": ") as $u
  | if ($s | startswith($h)) then $s[($h | length):]
    elif ($s | startswith($u)) then $s[($u | length):]
    else $s end;
# 改行・制御文字（U+0000-001F, U+007F）を 1 文字ずつ空白 1 個に置き換える。
# U+0085・U+2028・U+2029 も行区切りとして解釈されうるため同様に扱う。
# コードポイント単位（explode/implode）で処理し、連続する空白はまとめない。
def replace_controls:
  explode
  | map(if (. <= 31 or . == 127 or . == 133 or . == 8232 or . == 8233)
        then 32 else . end)
  | implode;
def truncate300(s):
  if (s | length) > 300 then (s[0:299] + "…") else s end;
# id に "[" "]"・空白・制御文字が含まれると行や id を偽装できてしまうため、
# そうした id を持つ記憶は本文に含めない（省略件数には数える）。
# 範囲指定には \x を使う: jq(Oniguruma) の \uXXXX は文字クラス内では
# コードポイント範囲として正しく解釈されず、無関係な ASCII 文字まで
# 誤って一致してしまうため。
def has_forbidden_id_chars:
  test("[\\[\\]\\x00-\\x1f\\x7f]") or test("\\s");

# 1つの JSON オブジェクトで ok=true、memories が「オブジェクトの配列で
# 各要素の id/summary が文字列」という形を満たさない場合はすべて失敗
# 扱いにする（一部の要素だけを間引くのではなく、応答全体を信用しない）。
if (length != 1) then error("hook: expected exactly one JSON document") end
| .[0] as $resp
| if ($resp.ok != true) then error("hook: ok is not true") end
| ($resp.memories) as $raw
| if ($raw | type) != "array" then error("hook: memories is not an array") end
| if (
    $raw
    | any(
        type != "object"
        or (has("id") | not)
        or ((.id | type) != "string")
        or (has("summary") | not)
        or ((.summary | type) != "string")
      )
  ) then error("hook: a memory has the wrong shape") end
| ($raw | length) as $raw_n
| if $raw_n == 0 then
    empty
  else
    ($raw | sort_by(.id)) as $sorted
    | [
        $sorted[]
        | select(.id | has_forbidden_id_chars | not)
        | (.id | base_key) as $k
        | (strip_prefix($k; .summary) | replace_controls) as $replaced
        # 接頭辞除去・置換後に空、または空白だけになった記憶は偽の方針を
        # 埋め込めてしまうため本文に含めない（省略件数には数える）。
        | select(($replaced | test("^ *$")) | not)
        | {id: .id, summary: truncate300($replaced)}
      ] as $items
    | ($items | map("- " + .summary + " [" + .id + "]")) as $lines
    | ($lines | length) as $m
    # 件数に対して二乗の探索にならないよう、候補ごとに文字列を組み立てて
    # 長さを測るのではなく、各行の長さの累積和を先に1回だけ計算し、
    # 候補の長さは累積和の差分（定数時間）で求める。
    | (reduce ($lines | map(length))[] as $l ([0]; . + [(.[-1]) + $l])) as $cumsum
    | (($HEADING | length) + ($INSTRUCTION | length)) as $heading_instruction_len
    | (
        [range($m; -1; -1)] as $ks
        | first(
            $ks[] as $k
            | ($raw_n - $k) as $omitted
            | ($heading_instruction_len + $cumsum[$k] + $k + 2) as $core_len
            | (
                if $omitted > 0 then
                  $core_len + 1 + ("（ほか \($omitted) 件は shared-memory search で取得）" | length)
                else
                  $core_len
                end
              ) as $candidate_len
            | select($candidate_len <= 2000)
            | $k
          )
      ) as $chosen_k
    | ($raw_n - $chosen_k) as $chosen_omitted
    | ([$HEADING, $INSTRUCTION, ""] + $lines[0:$chosen_k] | join("\n")) as $core
    | (
        if $chosen_omitted > 0 then
          $core + "\n" + "（ほか \($chosen_omitted) 件は shared-memory search で取得）"
        else
          $core
        end
      ) as $body
    | {hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $body}}
  end
JQ_FILTER

# CLI の実行と、その出力を読むすべての jq 処理（型の検証と本文の組み立て）
# を1つの timeout の内側にまとめる。段階ごとに別々の timeout を与えると
# 合計がフックの持ち時間（settings.json 側の10秒）を超えうるため。
#
# --foreground は付けない: この bash -c 全体を新しいプロセスグループで
# 実行させ、時間切れ時にグループごと止める（uv run が子として残す
# python や、jq 自体が居座るケースも含む）。子の stderr は常にファイルへ
# 捕捉し、呼び出し元へは転送しない（トレースバックにローカルパスが
# 含まれうるため）。
#
# --limit は十分大きな値を渡す: search サブコマンドに上限の検証は無く、
# 件数の絞り込み・並べ替え・省略件数の計算はすべて jq 側で行うため、
# ここで CLI 側のページングに件数を落とされると省略件数がずれる。
#
# シングルクォート内の $1 等は外側のシェルではなく内側の bash -c で展開
# させるためにそのままにしている（意図的な非展開）。
# shellcheck disable=SC2016
if ! "$timeout_cmd" "$timeout_value" bash -c '
  set -uo pipefail
  run_python="$1"; memory_cli="$2"; stdout_file="$3"; stderr_file="$4"
  heading="$5"; instruction="$6"; body_filter_file="$7"; output_file="$8"
  "$run_python" "$memory_cli" --require-vault search \
    --scope global --tag philosophy --limit 1000000 \
    >"$stdout_file" 2>"$stderr_file" </dev/null || exit 1
  jq -c -s --arg HEADING "$heading" --arg INSTRUCTION "$instruction" \
    -f "$body_filter_file" <"$stdout_file" >"$output_file" 2>/dev/null
' _ "$run_python" "$memory_cli" "$stdout_file" "$stderr_file" \
  "$heading" "$instruction" "$body_filter_file" "$output_file" 2>/dev/null; then
  emit_notice
fi

if [ -s "$output_file" ]; then
  cat "$output_file"
fi
exit 0
