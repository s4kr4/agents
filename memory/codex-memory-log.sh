#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <session-id> <role> <kind> [content] [importance]" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
memory_cli="${repo_root}/memory/memory.py"

session_id="$1"
role="$2"
kind="$3"
content="${4:-}"
importance="${5:-0.5}"

project_id="${LLM_MEMORY_PROJECT_ID:-$(basename "${PWD}")}"
user_id="${LLM_MEMORY_USER_ID:-default}"
client="${LLM_MEMORY_CLIENT:-codex}"
db_path="${LLM_MEMORY_DB:-${repo_root}/memory/memory.db}"

python3 "${memory_cli}" --db "${db_path}" append-event \
  --session-id "${session_id}" \
  --client "${client}" \
  --user-id "${user_id}" \
  --project-id "${project_id}" \
  --role "${role}" \
  --kind "${kind}" \
  --content "${content}" \
  --importance "${importance}"
