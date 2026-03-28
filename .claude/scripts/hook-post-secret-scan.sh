#!/usr/bin/env bash
# PostToolUse: Write/Edit 後にシークレットのハードコードを検出して警告
# 対応ルール: security.md

f=$(jq -r '.tool_input.file_path // empty')
if [ -z "$f" ] || [ ! -f "$f" ]; then exit 0; fi

if grep -qEi "(api[_-]?key|password|secret|token)[[:space:]]*=[[:space:]]*\S+" "$f" 2>/dev/null; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"WARNING: %s に機密情報がハードコードされている可能性があります。環境変数の使用を検討してください。"}}\n' "$f"
fi
