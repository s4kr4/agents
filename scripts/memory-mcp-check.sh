#!/bin/bash
set -euo pipefail

# Handshake-test the shared-memory MCP server (memory/check_mcp.py) against
# an isolated, throwaway Vault/local/queue/config -- never the caller's real
# storage or client config.
#
# This exists because check_mcp.py's live round trip actually calls
# write_memory(...), which writes a real observation/memory. Before this
# script, both deploy-memory-mcp.sh and the Makefile's memory-mcp-check
# target invoked check_mcp.py directly with no isolation: whatever
# LLM_MEMORY_VAULT/config the caller's shell happened to have (exactly the
# real Vault, if the user had already completed the README's "端末ごとの
# 保存先設定" step before running the installer) received a fake "editor"
# profile memory and a throwaway session on every first-time setup run, and
# Syncthing would then propagate it to every other machine. Conversely, on a
# machine with no Vault configured at all, create_server() raised
# MemoryConfigError and aborted the installer before it ever reached the
# registration step. Always redirecting to a fresh temp directory here fixes
# both: the check never depends on, or leaks into, the caller's real config.
#
# Usage: memory-mcp-check.sh [uv-path]
# AGENTSPATH may already be set by the caller (see Makefile); otherwise it
# is resolved from this script's own location, matching deploy-memory-mcp.sh.

AGENTSPATH="${AGENTSPATH:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

UV_PATH="${1:-}"
if [ -z "$UV_PATH" ]; then
    if ! UV_PATH="$(command -v uv)"; then
        echo 'skipped: uv is unavailable; install uv and retry' >&2
        exit 2
    fi
fi

tmp="$(mktemp -d)"
# EXIT alone misses SIGTERM/SIGINT/SIGHUP (e.g. an installer run that
# gets killed or Ctrl-C'd mid-check): observed leaving the temp dir
# behind under those signals, so they are trapped explicitly too.
trap 'rm -rf "$tmp"' EXIT INT TERM HUP

env -u LLM_MEMORY_CONFIG \
    LLM_MEMORY_VAULT="$tmp/vault" \
    LLM_MEMORY_LOCAL_DIR="$tmp/local" \
    LLM_MEMORY_QUEUE_DIR="$tmp/queue" \
    XDG_CONFIG_HOME="$tmp/config" \
    XDG_CACHE_HOME="$tmp/cache" \
    "$UV_PATH" run --locked --project "$AGENTSPATH/memory" python "$AGENTSPATH/memory/check_mcp.py"
