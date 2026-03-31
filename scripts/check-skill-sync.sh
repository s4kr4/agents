#!/usr/bin/env bash
set -euo pipefail

if [[ "${SKIP_SKILL_SYNC_CHECK:-}" == "1" ]]; then
    exit 0
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

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

declare -A staged_set=()
declare -A issues=()

while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    staged_set["$path"]=1
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

if [[ "${#issues[@]}" -eq 0 ]]; then
    exit 0
fi

echo "Skill/agent sync check failed."
echo "Changed Claude/Codex files must be mirrored on the other side before commit:"

for path in $(printf '%s\n' "${!issues[@]}" | sort); do
    echo "  - $path"
    echo "    ${issues[$path]}"
done

echo
echo "If this is intentional, bypass once with:"
echo "  SKIP_SKILL_SYNC_CHECK=1 git commit ..."

exit 1
