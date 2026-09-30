#!/usr/bin/env bash
set -euo pipefail

# 共有メモリ CLI の位置は HIKIDASHI_MCP_PATH だけで決める。既定値もフォールバック
# 探索も持たないのは、未設定の端末で意図しない clone を動かさないため。相対パスと
# 未展開のチルダは呼び出し元の作業ディレクトリ次第で別物を指すので受け付けない。
hikidashi_mcp_path="${HIKIDASHI_MCP_PATH:-}"
case "${hikidashi_mcp_path}" in
  /*) ;;
  *) hikidashi_mcp_path="" ;;
esac
memory_cli="${hikidashi_mcp_path}/memory.py"
run_python="${hikidashi_mcp_path}/run-python.sh"
if [ -z "${hikidashi_mcp_path}" ] || [ ! -d "${hikidashi_mcp_path}" ] ||
  [ ! -f "${memory_cli}" ] || [ ! -f "${run_python}" ] || [ ! -x "${run_python}" ]; then
  echo "HIKIDASHI_MCP_PATH must be an absolute path to a directory containing memory.py and an executable run-python.sh" >&2
  exit 1
fi

session_id="${1:-codex-$(date -u +%Y%m%dT%H%M%SZ)}"
project_id="${LLM_MEMORY_PROJECT_ID:-$(basename "${PWD}")}"
user_id="${LLM_MEMORY_USER_ID:-default}"
client="${LLM_MEMORY_CLIENT:-codex}"

"${run_python}" "${memory_cli}" start-session \
  --session-id "${session_id}" \
  --client "${client}" \
  --user-id "${user_id}" \
  --project-id "${project_id}"
