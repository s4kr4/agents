#!/usr/bin/env bash
# PreToolUse: Write/Edit がプロジェクトルート外を対象にしていたらブロック
# 対応ルール: file-operations.md
# 例外: ~/.claude/skills 配下への書き込みは許可
# 例外: ~/.claude/projects/*/memory/ 配下への書き込みは許可（auto memory 機能）
# 例外: ${TMPDIR:-/tmp}/claude-<UID>/<スラグ>/<session_id>/ 配下への書き込みは許可（スクラッチパッド）

# パスの前方一致と制御文字の除去をバイト単位で決定させる（ロケール非依存にする）
export LC_ALL=C

deny() {
  jq -nc --arg reason "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  exit 0
}

# stdin は一度だけ読む。file_path と session_id の両方をこのペイロードから取り出す
payload=$(cat)

if ! f=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null); then
  deny "入力JSONの解析に失敗しました"
fi
[ -z "$f" ] && exit 0

# realpath -m: 対象が存在しなくても正規化する。既定の realpath は失敗して空文字を返し、
# 前方一致の判定が "/*"（全許可）に化ける。-s は付けない（シンボリックリンクで境界を抜けられる）
real_f=$(realpath -m "$f")
# プロジェクトルートはフックを起動したプロセスの cwd ではなく CLAUDE_PROJECT_DIR を基準にする
real_project=$(realpath -m "${CLAUDE_PROJECT_DIR:-$PWD}")
real_skills=$(realpath -m "$HOME/.claude/skills")
real_home=$(realpath -m "$HOME")
scratch_base=$(realpath -m "${TMPDIR:-/tmp}/claude-$EUID")

case "$real_f" in
  "$real_project"/*|"$real_project") exit 0 ;;
  "$real_skills"/*|"$real_skills") exit 0 ;;
  # <スラグ>/<session_id>/<1 つ以上の要素> の深さを要求する（スラグ自体は照合しない）
  "$scratch_base"/*/*/*)
    session_id=$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null)
    # session_id が取れない場合はスクラッチパッドを許可しない（fail-closed）
    if [ -n "$session_id" ]; then
      rest=${real_f#"$scratch_base"/}
      after_slug=${rest#*/}
      # [ = ] のリテラル比較で突き合わせる（glob・正規表現として解釈させない）
      [ "${after_slug%%/*}" = "$session_id" ] && exit 0
    fi
    ;;
esac

# $real_home はクォートによりリテラル扱い（メタ文字を含む環境でも安全）
if [[ "$real_f" =~ ^"$real_home"/\.claude/projects/[^/]+/memory/ ]]; then
  exit 0
fi

f_safe=$(printf '%s' "$f" | tr -d '\000-\037')
deny "プロジェクト外への書き込みをブロック: ${f_safe}"
