#!/usr/bin/env bash
set -euo pipefail

if [[ "${SKIP_SKILL_SYNC_CHECK:-}" == "1" ]]; then
    exit 0
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
sync_script="$repo_root/scripts/sync-claude-codex-skills.sh"

map_counterpart() {
    local path="$1"
    local rel skill rest

    case "$path" in
        .claude/skills/INDEX.md|.claude/skills/_tracker/*|.claude/agents/INDEX.md)
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
            issues["$path"]="update and stage $counterpart"
        else
            issues["$path"]="create and stage $counterpart"
        fi
    done
}

stage_synced_files() {
    git add .claude/skills .claude/agents .codex/skills
}

collect_state

if [[ "${#issues[@]}" -eq 0 ]]; then
    exit 0
fi

if [[ "$has_claude_changes" -eq 1 && "$has_codex_changes" -eq 0 ]]; then
    echo "Auto-syncing skills from Claude to Codex..."
    "$sync_script" --from claude
    stage_synced_files
    collect_state
elif [[ "$has_codex_changes" -eq 1 && "$has_claude_changes" -eq 0 ]]; then
    echo "Auto-syncing skills from Codex to Claude..."
    "$sync_script" --from codex
    stage_synced_files
    collect_state
fi

if [[ "${#issues[@]}" -eq 0 ]]; then
    exit 0
fi

echo "Skill/agent sync check failed."

if [[ "$has_claude_changes" -eq 1 && "$has_codex_changes" -eq 1 ]]; then
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

echo
echo "Manual sync commands:"
echo "  scripts/sync-claude-codex-skills.sh --from claude"
echo "  scripts/sync-claude-codex-skills.sh --from codex"
echo
echo "If this is intentional, bypass once with:"
echo "  SKIP_SKILL_SYNC_CHECK=1 git commit ..."

exit 1
