#!/usr/bin/env bash
# PostToolUse: Write/Edit 後にシークレットのハードコードを検出して警告
# 入力を読めない・jq が使えない・入力を解析できない・対象を照合できないときは、
# スキャンできなかったことを stderr に出して exit 2 で終える
# 対応ルール: security.md
# 既知の限界: ファイル全文をスキャンするため、Write/Edit と無関係な既存行にも反応する。
#             また右辺の除外パターンはヒューリスティックであり、プレースホルダー表記を網羅できていない。
#             クォートなし・数字なしの英字のみの値（例: password ＝ MySecretPass）は
#             変数参照と区別できないため検出不可。
#             照合はバイト単位（LC_ALL=C）で行うため、= の前後の全角空白は空白として扱わず、
#             スマートクォート（“ ”）は除外パターンのクォートに当たらない。
#             警告の JSON にはパスをエスケープせずに埋め込むため、" などを含むパスでは JSON が壊れる。
#             NotebookEdit は対象外（settings.json の matcher が Write|Edit のため）。
#             macOS の BSD grep での \S の解釈は未確認。

# PostToolUse の exit 2 は書き込みを取り消せないが、stderr が Claude に表示される。
# スキャンを黙って省かず、実行できなかったことを伝えるために使う
scan_failed() {
  printf 'シークレットスキャンを実行できませんでした: %s\n' "$1" >&2
  exit 2
}

# 依存コマンドの確認より先に stdin を読み切る（読まずに終了すると、書き込み側がパイプの切断を受けうるため）
payload=$(cat) || scan_failed "標準入力を読み取れませんでした"

jq -n 'empty' >/dev/null 2>&1 \
  || scan_failed "jq を実行できません。jq をインストールし、Claude Code を起動する環境の PATH に入れてください"

f=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null) \
  || scan_failed "入力JSONの解析に失敗しました（jq の終了コード $?）"
if [ -z "$f" ] || [ ! -f "$f" ]; then exit 0; fi

# 検出対象: api_key / password / secret / token への代入
detect='(api[_-]?key|password|secret|token)[[:space:]]*=[[:space:]]*\S+'

# 除外対象（右辺が機密の直値ではないと判断できるパターン）:
#   1. $VAR, ${...}, $(...) などの動的参照
#   2. <PLACEHOLDER> のようなプレースホルダー表記
#   3. {{ template }} のようなテンプレート構文
#   4. 空文字列 '' / ""
#   5. your / example / dummy / placeholder / changeme / sample / xxx で始まる語
#   6. 右辺の最初のトークンがクォートなし・数字なしの識別子（コード式とみなす。
#      例: value, this.password, await 式）
exclude=(
  -e '=[[:space:]]*.?\$'
  -e '=[[:space:]]*.?<'
  -e '=[[:space:]]*.?\{\{'
  -e $'=[[:space:]]*(\x27\x27|"")[;]?[[:space:]]*$'
  -e '=[[:space:]]*.?(your|example|dummy|placeholder|changeme|sample|xxx)'
  -e '=[[:space:]]*[A-Za-z_.#][A-Za-z_.#]*([[:space:];,)]|$)'
)

# パイプラインの途中の失敗は終了コードに現れないため、段ごとに分けて終了コードを確かめる。
# grep の終了コードで、照合できたこと（0: 一致あり、1: 一致なし）と、
# 読み取れない・grep が無いなどで照合できなかったこと（2 以上）を区別する。
# LC_ALL=C: 照合をバイト単位にして、結果を起動時のロケールに依存させない。UTF-8 ロケールでは
# Shift_JIS や Latin-1 のバイト列が \S などの文字クラスに当たらず、値の先頭にあると見逃すため
# -a: NUL を含むファイルは C ロケールでもバイナリとみなされ、照合結果が出ないため、テキストとして照合させる
# 除外の照合にも LC_ALL=C を付ける。こちらの -a は、入力が NUL を含まない（bash の変数に入らない）ため
# 現状は結果を変えないが、1 段目と照合の条件をそろえるために付ける
# NUL を含む行では bash がコマンド置換の警告を stderr に出すため、置換ごと抑止する。
# NUL は落とされたうえで除外パターンと照合される（例: password=<NUL>abc は password=abc とみなされ、除外に当たる）
{ matches=$(LC_ALL=C grep -aEi "$detect" "$f" 2>/dev/null); } 2>/dev/null
grep_status=$?
[ "$grep_status" -le 1 ] || scan_failed "$f を照合できませんでした（grep の終了コード $grep_status）"
[ "$grep_status" -eq 0 ] || exit 0

suspects=$(printf '%s\n' "$matches" | LC_ALL=C grep -aEvi "${exclude[@]}" 2>/dev/null)
filter_status=$?
[ "$filter_status" -le 1 ] || scan_failed "$f の除外パターンを照合できませんでした（grep の終了コード $filter_status）"

if [ -n "$suspects" ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"WARNING: %s に機密情報がハードコードされている可能性があります。環境変数の使用を検討してください。"}}\n' "$f"
fi

exit 0
