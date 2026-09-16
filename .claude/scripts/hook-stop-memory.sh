#!/usr/bin/env bash
set -euo pipefail

input=$(cat)

# 共有メモリ CLI の位置は MEMORY_MCP_PATH だけで決める。既定値もフォールバック
# 探索も持たないのは、未設定の端末で意図しない clone を動かさないため。相対パスと
# 未展開のチルダは呼び出し元の作業ディレクトリ次第で別物を指すので受け付けない。
# Stop フックは停止をブロックしてはならないので、使えないときは何も言わず exit 0。
memory_mcp_path="${MEMORY_MCP_PATH:-}"
case "$memory_mcp_path" in
  /*) ;;
  *) exit 0 ;;
esac
memory_cli="${memory_mcp_path}/memory.py"
run_python="${memory_mcp_path}/run-python.sh"
if [ ! -d "$memory_mcp_path" ] || [ ! -f "$memory_cli" ] ||
  [ ! -f "$run_python" ] || [ ! -x "$run_python" ]; then
  exit 0
fi

session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
cwd=$(printf '%s' "$input" | jq -r '.cwd // ""')
transcript_path=$(printf '%s' "$input" | jq -r '.transcript_path // empty')

if [ -z "$session_id" ] || [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
  exit 0
fi

project_id=$(basename "${cwd:-default}")

last_user_text=$(jq -rs '
  map(select(.type == "user"))
  | last
  | .message.content
  | if type == "string" then .
    elif type == "array" then map(select(.type == "text") | .text) | join("\n")
    else ""
    end
' "$transcript_path" 2>/dev/null || printf '')

last_assistant_text=$(jq -rs '
  map(select(.type == "assistant"))
  | last
  | .message.content
  | if type == "string" then .
    elif type == "array" then map(select(.type == "text") | .text) | join("\n")
    else ""
    end
' "$transcript_path" 2>/dev/null || printf '')

summary_text=$(jq -rs '
  . as $all
  | [
      $all[]
      | select(.type == "user" or .type == "assistant")
      | .type as $role
      | .message.content
      | if type == "string" then .
        elif type == "array" then map(select(.type == "text") | .text) | join("\n")
        else ""
        end
      | select(length > 0)
      | gsub("\\s+"; " ")
      | if length > 120 then .[:117] + "..." else . end
      | "\($role): \(.)"
    ]
  | .[-6:]
  | join(" / ")
' "$transcript_path" 2>/dev/null || printf '')

# 直接 local/Vault への書き込みを試み、失敗したら queue にフォールバック
{
  "$run_python" "$memory_cli" start-session \
    --session-id "$session_id" \
    --client "claude-code" \
    --user-id "default" \
    --project-id "$project_id" >/dev/null 2>&1 &&

  if [ -n "$last_user_text" ]; then
    "$run_python" "$memory_cli" append-event \
      --session-id "$session_id" \
      --client "claude-code" \
      --user-id "default" \
      --project-id "$project_id" \
      --role "user" \
      --kind "message" \
      --content "$last_user_text" >/dev/null 2>&1
  fi &&

  if [ -n "$last_assistant_text" ]; then
    "$run_python" "$memory_cli" append-event \
      --session-id "$session_id" \
      --client "claude-code" \
      --user-id "default" \
      --project-id "$project_id" \
      --role "assistant" \
      --kind "message" \
      --content "$last_assistant_text" >/dev/null 2>&1
  fi &&

  "$run_python" "$memory_cli" end-session \
    --session-id "$session_id" \
    --summary "${summary_text:-session:${session_id}}" \
    --append-summary-event \
    --extract \
    --consolidate >/dev/null 2>&1
} || {
  # local/Vault への書き込み失敗 → ファイルキューにフォールバック
  "$run_python" "$memory_cli" queue-session \
    --session-id "$session_id" \
    --client "claude-code" \
    --user-id "default" \
    --project-id "$project_id" \
    --user-content "$last_user_text" \
    --assistant-content "$last_assistant_text" \
    --summary "${summary_text:-session:${session_id}}" >/dev/null 2>&1 || true
}

exit 0
