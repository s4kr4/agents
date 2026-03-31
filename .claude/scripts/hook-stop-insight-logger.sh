#!/usr/bin/env bash
# Stop: アシスタント応答から Insight ブロックを抽出してログに記録する

input=$(cat)

debug_dir="$(dirname "$0")/../skills/_tracker"
mkdir -p "$debug_dir"

timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
cwd=$(printf '%s' "$input" | jq -r '.cwd // ""')

tracker_dir="${cwd}/.claude/skills/_tracker"
mkdir -p "$tracker_dir"

# デバッグログ
printf '%s called: keys=%s transcript_path=%s\n' \
  "$timestamp" \
  "$(printf '%s' "$input" | jq -r 'keys | join(",")' 2>/dev/null)" \
  "$(printf '%s' "$input" | jq -r '.transcript_path // "(none)"' 2>/dev/null)" \
  >> "$debug_dir/debug-stop.log"

transcript_path=$(printf '%s' "$input" | jq -r '.transcript_path // empty')
if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
  exit 0
fi

last_assistant_text=$(jq -rs '
  . as $all
  | ([
      range(length) | . as $i | $all[$i]
      | select(.type == "user")
      | select(
          (.message.content | type) == "string" or
          ((.message.content[0].type? // "") != "tool_result")
        )
      | $i
    ] | last // -1) as $last_user_idx
  | $all[($last_user_idx+1):]
  | map(select(.type == "assistant")
      | .message.content[]?
      | select(.type == "text")
      | .text)
  | join("\n")
' "$transcript_path" 2>/dev/null) || exit 0

[ -z "$last_assistant_text" ] && exit 0

insights_json=$(printf '%s' "$last_assistant_text" | awk '
  /^`★ Insight/ { in_block=1; buf=""; next }
  in_block && substr($0, 1, 1) == "`" {
    if (buf != "") {
      if (printed) printf "\x1e"
      printf "%s", buf
      printed=1
    }
    in_block=0; buf=""; next
  }
  in_block {
    if (buf == "") buf = $0
    else buf = buf "\n" $0
  }
' | jq -Rs 'split("\u001e") | map(select(length > 0) | ltrimstr("\n") | rtrimstr("\n"))') || exit 0

count=$(printf '%s' "$insights_json" | jq 'length') || exit 0
[ "$count" -eq 0 ] && exit 0

jq -nc \
  --arg ts "$timestamp" \
  --arg sid "$session_id" \
  --arg cwd "$cwd" \
  --argjson ins "$insights_json" \
  '{timestamp: $ts, session_id: $sid, cwd: $cwd, insights: $ins}' \
  >> "$tracker_dir/insights.jsonl"

exit 0
