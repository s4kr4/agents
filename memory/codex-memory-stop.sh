#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <session-id> [summary]" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
memory_cli="${repo_root}/memory/memory.py"

session_id="$1"
summary="${2:-}"
db_path="${LLM_MEMORY_DB:-${repo_root}/memory/memory.db}"

cmd=(
  python3 "${memory_cli}" --db "${db_path}" end-session
  --session-id "${session_id}"
  --append-summary-event
  --extract
  --consolidate
)

if [ -n "${summary}" ]; then
  cmd+=(--summary "${summary}")
fi

"${cmd[@]}"
