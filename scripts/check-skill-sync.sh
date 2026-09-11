#!/usr/bin/env bash
# ステージされた .claude / .codex のスキル・エージェントが揃っているかを検査する。
#
# 契約:
#   既定                    : 検査のみ。ファイルの書き込みも git add も一切行わない。
#                             不整合があれば解消手順を表示して exit 1 する。
#   --auto-sync             : 片側だけがステージされている場合に同期スクリプトを
#                             起動し、生成されたカウンターパートを git add する。
#                             このオプションを渡したときだけ書き込みが発生する。
#   SKIP_SKILL_SYNC_CHECK=1 : 引数の有無を問わず何もせず exit 0 する。
#
# 名前が check- で始まる以上、既定で書き込まない性質は崩さないこと。
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/check-skill-sync.sh [--auto-sync]

Options:
  --auto-sync  Sync the counterpart files and stage them instead of only
               reporting the inconsistencies.
  -h, --help   Show this help.

Environment:
  SKIP_SKILL_SYNC_CHECK=1  Skip the check entirely and exit 0.
EOF
}

# bypass は「何があっても素通しする」契約なので、引数解析より前に効かせる。
if [[ "${SKIP_SKILL_SYNC_CHECK:-}" == "1" ]]; then
    exit 0
fi

auto_sync=0

# 引数は副作用を起こす前に全部読み切る。途中で同期を始めると、後続の不明な
# 引数で拒否する前に書き込みが発生してしまう。
while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto-sync)
            auto_sync=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
sync_script="$repo_root/scripts/sync-claude-codex-skills.sh"

map_counterpart() {
    local path="$1"
    local rel skill rest

    case "$path" in
        .claude/skills/_tracker/*)
            return 1
            ;;
        .claude/skills/*)
            rel="${path#.claude/skills/}"
            skill="${rel%%/*}"
            rest="${rel#"$skill"/}"

            if [[ "$rel" == "$skill" ]]; then
                return 1
            fi

            printf '.codex/skills/%s/%s\n' "$skill" "$rest"
            ;;
        .claude/agents/*.md)
            skill="${path##*/}"
            skill="${skill%.md}"
            printf '.codex/skills/%s/SKILL.md\n' "$skill"
            ;;
        .codex/skills/*)
            rel="${path#.codex/skills/}"
            skill="${rel%%/*}"
            rest="${rel#"$skill"/}"

            if [[ "$skill" == ".system" || "$rel" == "$skill" ]]; then
                return 1
            fi

            if [[ -e ".claude/skills/$skill" ]]; then
                printf '.claude/skills/%s/%s\n' "$skill" "$rest"
            elif [[ -e ".claude/agents/$skill.md" ]]; then
                printf '.claude/agents/%s.md\n' "$skill"
            else
                printf '.claude/skills/%s/%s\n' "$skill" "$rest"
            fi
            ;;
        *)
            return 1
            ;;
    esac
}

collect_state() {
    declare -gA staged_set=()
    declare -gA issues=()
    declare -g has_claude_changes=0
    declare -g has_codex_changes=0

    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        staged_set["$path"]=1

        case "$path" in
            .claude/skills/*|.claude/agents/*)
                has_claude_changes=1
                ;;
            .codex/skills/*)
                has_codex_changes=1
                ;;
        esac
    done < <(git diff --cached --name-only --diff-filter=ACMRD)

    for path in "${!staged_set[@]}"; do
        counterpart="$(map_counterpart "$path" || true)"
        [[ -n "$counterpart" ]] || continue

        if [[ -n "${staged_set[$counterpart]+x}" ]]; then
            continue
        fi

        if [[ -e "$counterpart" || -L "$counterpart" ]]; then
            # sync 実行後、カウンターパートが HEAD と同一なら同期済みとみなす
            if git diff --quiet HEAD -- "$counterpart" 2>/dev/null; then
                continue
            fi
            issues["$path"]="update and stage $counterpart"
        else
            # ソースの削除がステージ済みで、対応先がインデックス上にも存在しないなら
            # 両側不在＝同期済みとみなす
            if ! git diff --cached --quiet --diff-filter=D -- "$path" \
                && ! git ls-files --error-unmatch "$counterpart" >/dev/null 2>&1; then
                continue
            fi
            issues["$path"]="create and stage $counterpart"
        fi
    done
}

collect_state

if [[ "${#issues[@]}" -eq 0 ]]; then
    exit 0
fi

if [[ "$auto_sync" -eq 1 && "$has_claude_changes" -eq 1 && "$has_codex_changes" -eq 0 ]]; then
    echo "Auto-syncing skills from Claude to Codex..."
    "$sync_script" --from claude
    for path in "${!issues[@]}"; do
        counterpart="$(map_counterpart "$path" || true)"
        [[ -n "$counterpart" && -e "$counterpart" ]] && git add "$counterpart"
    done
    collect_state
elif [[ "$auto_sync" -eq 1 && "$has_codex_changes" -eq 1 && "$has_claude_changes" -eq 0 ]]; then
    echo "Auto-syncing skills from Codex to Claude..."
    "$sync_script" --from codex
    for path in "${!issues[@]}"; do
        counterpart="$(map_counterpart "$path" || true)"
        [[ -n "$counterpart" && -e "$counterpart" ]] && git add "$counterpart"
    done
    collect_state
fi

if [[ "${#issues[@]}" -eq 0 ]]; then
    exit 0
fi

echo "Skill/agent sync check failed."

if [[ "$auto_sync" -eq 0 ]]; then
    echo "This run only inspected the staged files; nothing was written or staged."
elif [[ "$has_claude_changes" -eq 1 && "$has_codex_changes" -eq 1 ]]; then
    echo "Both Claude and Codex skill/agent files are staged in the same commit."
    echo "Automatic sync is skipped because the source of truth is ambiguous."
else
    echo "Automatic sync ran, but staged files are still inconsistent."
fi

echo "Remaining paths that need manual attention:"

for path in $(printf '%s\n' "${!issues[@]}" | sort); do
    echo "  - $path"
    echo "    ${issues[$path]}"
done

if [[ "$auto_sync" -eq 0 ]]; then
    echo
    echo "Let this script sync and stage the counterparts for you:"
    echo "  scripts/check-skill-sync.sh --auto-sync"
fi

echo
echo "Manual sync commands:"
echo "  scripts/sync-claude-codex-skills.sh --from claude"
echo "  scripts/sync-claude-codex-skills.sh --from codex"
echo
echo "If this is intentional, bypass once with:"
echo "  SKIP_SKILL_SYNC_CHECK=1 git commit ..."

exit 1
