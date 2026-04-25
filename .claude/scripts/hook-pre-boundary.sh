#!/usr/bin/env bash
# PreToolUse: Write/Edit がプロジェクトルート外を対象にしていたらブロック
# 対応ルール: file-operations.md
# 例外: ~/.claude/skills 配下への書き込みは許可
# 例外: ~/.claude/projects/*/memory/ 配下への書き込みは許可（auto memory 機能）

if ! f=$(jq -r '.tool_input.file_path // empty' 2>/dev/null); then
  jq -nc '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:"入力JSONの解析に失敗しました"}}'
  exit 0
fi
[ -z "$f" ] && exit 0

real_f=$(realpath -m "$f")
real_cwd=$(realpath "$PWD")
real_skills=$(realpath "$HOME/.claude/skills")
real_home=$(realpath "$HOME")

case "$real_f" in
  "$real_cwd"/*|"$real_cwd") exit 0 ;;
  "$real_skills"/*|"$real_skills") exit 0 ;;
esac

# TODO: $real_home に正規表現メタ文字が含まれる環境では誤マッチの可能性あり。パターン展開方式への移行を検討。
if [[ "$real_f" =~ ^${real_home}/\.claude/projects/[^/]+/memory/ ]]; then
  exit 0
fi

f_safe=$(printf '%s' "$f" | tr -d '\000-\037')
jq -nc --arg reason "プロジェクト外への書き込みをブロック: ${f_safe}" \
  '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
