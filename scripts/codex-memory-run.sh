#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
start_script="${script_dir}/codex-memory-start.sh"
stop_script="${script_dir}/codex-memory-stop.sh"

# 共有メモリ CLI の位置は MEMORY_MCP_PATH だけで決める。既定値もフォールバック
# 探索も持たないのは、未設定の端末で意図しない clone を動かさないため。相対パスと
# 未展開のチルダは呼び出し元の作業ディレクトリ次第で別物を指すので受け付けない。
# 検査は mktemp と trap より前に置く: 後ろに置くと、start.sh が同じ理由で失敗
# したときに一時ファイルの後始末と二重のエラー出力が先に走ってしまう。
memory_mcp_path="${MEMORY_MCP_PATH:-}"
case "${memory_mcp_path}" in
  /*) ;;
  *) memory_mcp_path="" ;;
esac
if [ -z "${memory_mcp_path}" ] || [ ! -d "${memory_mcp_path}" ] ||
  [ ! -f "${memory_mcp_path}/memory.py" ] ||
  [ ! -f "${memory_mcp_path}/run-python.sh" ] ||
  [ ! -x "${memory_mcp_path}/run-python.sh" ]; then
  echo "MEMORY_MCP_PATH must be an absolute path to a directory containing memory.py and an executable run-python.sh" >&2
  exit 1
fi

session_id="${LLM_MEMORY_SESSION_ID:-codex-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
summary_file="$(mktemp)"
codex_exit_code=0

# trap EXIT から呼ばれるため、shellcheck からは到達不能に見える（SC2317）。
# shellcheck disable=SC2317
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
