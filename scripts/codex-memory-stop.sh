#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <session-id> [summary]" >&2
  exit 1
fi

# 共有メモリ CLI の位置は MEMORY_MCP_PATH だけで決める。既定値もフォールバック
# 探索も持たないのは、未設定の端末で意図しない clone を動かさないため。相対パスと
# 未展開のチルダは呼び出し元の作業ディレクトリ次第で別物を指すので受け付けない。
memory_mcp_path="${MEMORY_MCP_PATH:-}"
case "${memory_mcp_path}" in
  /*) ;;
  *) memory_mcp_path="" ;;
esac
memory_cli="${memory_mcp_path}/memory.py"
run_python="${memory_mcp_path}/run-python.sh"
if [ -z "${memory_mcp_path}" ] || [ ! -d "${memory_mcp_path}" ] ||
  [ ! -f "${memory_cli}" ] || [ ! -f "${run_python}" ] || [ ! -x "${run_python}" ]; then
  echo "MEMORY_MCP_PATH must be an absolute path to a directory containing memory.py and an executable run-python.sh" >&2
  exit 1
fi

session_id="$1"
summary="${2:-}"

cmd=(
  "${run_python}" "${memory_cli}" end-session
  --session-id "${session_id}"
  --append-summary-event
  --extract
  --consolidate
)

if [ -n "${summary}" ]; then
  cmd+=(--summary "${summary}")
fi

"${cmd[@]}"
