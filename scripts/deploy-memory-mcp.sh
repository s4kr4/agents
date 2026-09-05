#!/bin/bash
set -euo pipefail

AGENTSPATH="${AGENTSPATH:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
if ! UV_PATH="$(command -v uv)"; then
    echo 'skipped: uv is unavailable; install uv and retry' >&2
    exit 2
fi
UV_PATH="$(cd -- "$(dirname -- "$UV_PATH")" && pwd)/$(basename -- "$UV_PATH")"
"$UV_PATH" sync --locked --project "$AGENTSPATH/memory" >&2
# Isolated: never the caller's real Vault/config -- see memory-mcp-check.sh.
AGENTSPATH="$AGENTSPATH" bash "$AGENTSPATH/scripts/memory-mcp-check.sh" "$UV_PATH" >&2
exec "$UV_PATH" run --locked --project "$AGENTSPATH/memory" python \
    "$AGENTSPATH/scripts/memory_mcp_config.py" --uv "$UV_PATH" --agents-path "$AGENTSPATH" "$@"
