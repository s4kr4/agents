#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
memory_cli="${repo_root}/memory/memory.py"

session_id="${1:-codex-$(date -u +%Y%m%dT%H%M%SZ)}"
project_id="${LLM_MEMORY_PROJECT_ID:-$(basename "${PWD}")}"
user_id="${LLM_MEMORY_USER_ID:-default}"
client="${LLM_MEMORY_CLIENT:-codex}"
db_path="${LLM_MEMORY_DB:-${repo_root}/memory/memory.db}"

python3 "${memory_cli}" --db "${db_path}" start-session \
  --session-id "${session_id}" \
  --client "${client}" \
  --user-id "${user_id}" \
  --project-id "${project_id}"
