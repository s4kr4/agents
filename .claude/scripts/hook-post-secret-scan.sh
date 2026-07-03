#!/usr/bin/env bash
# PostToolUse: Write/Edit 後にシークレットのハードコードを検出して警告
# 対応ルール: security.md
# 既知の限界: ファイル全文をスキャンするため、Write/Edit と無関係な既存行にも反応する。
#             また右辺の除外パターンはヒューリスティックであり、プレースホルダー表記を網羅できていない。
#             クォートなし・数字なしの英字のみの値（例: password ＝ MySecretPass）は
#             変数参照と区別できないため検出不可。

f=$(jq -r '.tool_input.file_path // empty')
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

if grep -Ei "$detect" "$f" 2>/dev/null | grep -Evi "${exclude[@]}" | grep -q .; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"WARNING: %s に機密情報がハードコードされている可能性があります。環境変数の使用を検討してください。"}}\n' "$f"
fi

exit 0
