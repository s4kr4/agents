#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
start_script="${repo_root}/memory/codex-memory-start.sh"
stop_script="${repo_root}/memory/codex-memory-stop.sh"

session_id="${LLM_MEMORY_SESSION_ID:-codex-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
summary_file="$(mktemp)"
codex_exit_code=0

cleanup() {
  if [ -s "${summary_file}" ]; then
    "${stop_script}" "${session_id}" "$(cat "${summary_file}")" >/dev/null 2>&1 || true
  else
    "${stop_script}" "${session_id}" >/dev/null 2>&1 || true
  fi
  rm -f "${summary_file}"
}

trap cleanup EXIT

"${start_script}" "${session_id}" >/dev/null

if ! command -v codex >/dev/null 2>&1; then
  echo "codex command not found" >&2
  exit 127
fi

set +e
if [ -t 0 ] && [ -t 1 ]; then
  codex "$@"
  codex_exit_code=$?
else
  codex "$@" | tee "${summary_file}"
  codex_exit_code=${PIPESTATUS[0]}
fi
set -e

exit "${codex_exit_code}"
