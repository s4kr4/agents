#!/usr/bin/env bash
# Stop: セッション中に使用されたスキルのフィードバックファイルを自動生成する
# transcript からスキル関連シグナル（不満表現・やり直し・Skill呼び出し前後）を抽出

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

# --- シグナル抽出 ---

# 1. 不満・やり直しシグナルを含むユーザーメッセージを抽出
dissatisfaction_signals=$(jq -r '
  select(.type == "user")
  | .message.content
  | if type == "string" then .
    elif type == "array" then map(select(.type == "text") | .text) | join("")
    else ""
    end
  | select(
      test("やり直し|違う|うまくいかない|動かない|おかしい|間違|ダメ|だめ|修正して|直して|そうじゃな|not working|wrong|fix|redo|broken|doesn.t work"; "i")
    )
' "$transcript_path" 2>/dev/null | head -c 2000)

# 2. Skill 呼び出し直後のユーザー応答を抽出（承認/却下/修正指示のシグナル）
# uuid を使って Skill 呼び出し後の最初のユーザーメッセージを取得
post_skill_responses=$(jq -rs '
  . as $all
  | [range(length)] as $indices
  | [
      $indices[] | . as $i
      | $all[$i]
      | select(.type == "assistant")
      | .message.content[]?
      | select(.type == "tool_use" and .name == "Skill")
      | $i
    ] as $skill_indices
  | [
      $skill_indices[] | . as $si
      | [
          range($si + 1; ($si + 10; length) | if . > length then length else . end)
          | . as $j | $all[$j]
          | select(.type == "user")
        ][0]
      | .message.content
      | if type == "string" then .
        elif type == "array" then map(select(.type == "text") | .text) | join("")
        else ""
        end
    ]
  | map(select(length > 0))
  | join("\n---\n")
' "$transcript_path" 2>/dev/null | head -c 2000)

# 3. ユーザーの最初と最後のメッセージ（タスクの概要と結論）
first_user_msg=$(jq -r '
  select(.type == "user")
  | .message.content
  | if type == "string" then .
    elif type == "array" then map(select(.type == "text") | .text) | join("")
    else ""
    end
' "$transcript_path" 2>/dev/null | head -c 500)

last_user_msg=$(jq -r '
  select(.type == "user")
  | .message.content
  | if type == "string" then .
    elif type == "array" then map(select(.type == "text") | .text) | join("")
    else ""
    end
' "$transcript_path" 2>/dev/null | tail -c 500)

# --- フィードバック品質の判定 ---
has_signals="false"
if [ -n "$dissatisfaction_signals" ] || [ -n "$post_skill_responses" ]; then
  has_signals="true"
fi

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
has_signals: ${has_signals}
---

## 使用コンテキスト
cwd: ${cwd}
branch: ${git_branch}
最近のコミット:
${git_log}

## ユーザータスク
### 最初のリクエスト
${first_user_msg}

### 最後のメッセージ
${last_user_msg}

## 不満・やり直しシグナル
${dissatisfaction_signals:-なし}

## Skill呼び出し後のユーザー応答
${post_skill_responses:-なし}
MARKDOWN

  # usage.jsonl への記録
  tracker_dir="${cwd}/.claude/skills/_tracker"
  mkdir -p "$tracker_dir"
  printf '{"timestamp":"%s","skill":"%s","cwd":"%s","session_id":"%s","has_signals":%s}\n' \
    "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$skill" "$cwd" "$session_id" "$has_signals" \
    >> "$tracker_dir/usage.jsonl"

done <<< "$skills"

exit 0
