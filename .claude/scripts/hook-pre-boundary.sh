#!/usr/bin/env bash
# PreToolUse: Write/Edit がプロジェクトルート外を対象にしていたらブロック
# 対応ルール: file-operations.md

f=$(jq -r '.tool_input.file_path // empty')
[ -z "$f" ] && exit 0

real_f=$(realpath -m "$f")
real_cwd=$(realpath "$PWD")

case "$real_f" in
  "$real_cwd"/*|"$real_cwd") exit 0 ;;
  *) printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"プロジェクト外への書き込みをブロック: %s"}}\n' "$f" ;;
esac
