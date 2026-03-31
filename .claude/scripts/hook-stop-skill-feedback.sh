#!/usr/bin/env bash
# Stop: セッション中に使用されたスキルのフィードバックファイルを自動生成する

input=$(cat)

session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
cwd=$(printf '%s' "$input" | jq -r '.cwd // ""')
transcript_path=$(printf '%s' "$input" | jq -r '.transcript_path // empty')

# transcript_path が存在する場合のみ処理（Stop フック）
if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
  exit 0
fi
skills=$(jq -r '
  select(.type == "assistant")
  | .message.content[]?
  | select(.type == "tool_use" and .name == "Skill")
  | .input.skill
' "$transcript_path" 2>/dev/null | sort -u) || exit 0

[ -z "$skills" ] && exit 0

# ユーザーメッセージを抽出
all_user_messages=$(jq -r '
  select(.type == "user")
  | .message.content
  | if type == "string" then .
    elif type == "array" then map(select(.type == "text") | .text) | join("")
    else ""
    end
' "$transcript_path" 2>/dev/null) || exit 0

user_messages_excerpt=$(printf '%s' "$all_user_messages" | head -c 2000)

# git コンテキスト取得
git_branch=$(cd "$cwd" && git branch --show-current 2>/dev/null)
git_log=$(cd "$cwd" && git log --oneline -5 2>/dev/null)

today=$(date +"%Y-%m-%d")

while IFS= read -r skill; do
  [ -z "$skill" ] && continue

  feedback_dir="${cwd}/.claude/skills/${skill}/feedback"

  mkdir -p "$feedback_dir"

  # 同セッションの feedback が既に存在すればスキップ
  if grep -rl "session_id: ${session_id}" "$feedback_dir" 2>/dev/null | grep -q .; then
    continue
  fi

  # 当日の既存ファイル数から連番を決定（applied/ サブディレクトリを除く）
  existing_count=$(find "$feedback_dir" -maxdepth 1 -name "${today}-*.md" 2>/dev/null | wc -l)
  n=$((existing_count + 1))

  filepath="${feedback_dir}/${today}-${n}.md"

  cat > "$filepath" << MARKDOWN
---
skill: ${skill}
date: ${today}
session_id: ${session_id}
auto_generated: true
---

## 使用コンテキスト
cwd: ${cwd}
branch: ${git_branch}
最近のコミット:
${git_log}

## ユーザータスク
${user_messages_excerpt}

## 良かった点
（自動収集不可 - /skill-improve で分析）

## 問題のあったセクション
（自動収集不可 - /skill-improve で分析）

## 改善提案
（自動収集不可 - /skill-improve で分析）

## 参考例・リンク
なし
MARKDOWN

  # usage.jsonl への記録
  tracker_dir="${cwd}/.claude/skills/_tracker"
  mkdir -p "$tracker_dir"
  printf '{"timestamp":"%s","skill":"%s","cwd":"%s","session_id":"%s"}\n' \
    "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$skill" "$cwd" "$session_id" \
    >> "$tracker_dir/usage.jsonl"

done <<< "$skills"

exit 0
