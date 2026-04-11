#!/usr/bin/env bash
set -euo pipefail

input=$(cat)

repo_root="${HOME}/.agents"
memory_cli="${repo_root}/memory/memory.py"
db_path="${LLM_MEMORY_DB:-${repo_root}/memory/memory.db}"

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

# 直接DB書き込みを試み、失敗したら queue にフォールバック
{
  python3 "$memory_cli" --db "$db_path" start-session \
    --session-id "$session_id" \
    --client "claude-code" \
    --user-id "default" \
    --project-id "$project_id" >/dev/null 2>&1 &&

  if [ -n "$last_user_text" ]; then
    python3 "$memory_cli" --db "$db_path" append-event \
      --session-id "$session_id" \
      --client "claude-code" \
      --user-id "default" \
      --project-id "$project_id" \
      --role "user" \
      --kind "message" \
      --content "$last_user_text" >/dev/null 2>&1
  fi &&

  if [ -n "$last_assistant_text" ]; then
    python3 "$memory_cli" --db "$db_path" append-event \
      --session-id "$session_id" \
      --client "claude-code" \
      --user-id "default" \
      --project-id "$project_id" \
      --role "assistant" \
      --kind "message" \
      --content "$last_assistant_text" >/dev/null 2>&1
  fi &&

  python3 "$memory_cli" --db "$db_path" end-session \
    --session-id "$session_id" \
    --summary "${summary_text:-session:${session_id}}" \
    --append-summary-event \
    --extract \
    --consolidate >/dev/null 2>&1
} || {
  # DB書き込み失敗 → ファイルキューにフォールバック
  python3 "$memory_cli" queue-session \
    --session-id "$session_id" \
    --client "claude-code" \
    --user-id "default" \
    --project-id "$project_id" \
    --user-content "$last_user_text" \
    --assistant-content "$last_assistant_text" \
    --summary "${summary_text:-session:${session_id}}" >/dev/null 2>&1 || true
}

exit 0
