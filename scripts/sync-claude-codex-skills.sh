#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/sync-claude-codex-skills.sh --from claude [--dry-run]
  scripts/sync-claude-codex-skills.sh --from codex [--dry-run]

Options:
  --from <claude|codex>  Sync direction.
  --dry-run              Print planned actions without writing files.
EOF
}

direction=""
dry_run=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from)
            direction="${2:-}"
            shift 2
            ;;
        --dry-run)
            dry_run=1
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

if [[ "$direction" != "claude" && "$direction" != "codex" ]]; then
    echo "--from must be claude or codex" >&2
    usage >&2
    exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

copy_dir() {
    local src="$1"
    local dst="$2"

    echo "sync dir: $src -> $dst"

    if [[ "$dry_run" -eq 1 ]]; then
        return 0
    fi

    rm -rf "$dst"
    mkdir -p "$dst"
    cp -R "$src"/. "$dst"/
}

extract_body() {
    local file="$1"
    awk 'BEGIN{n=0} /^---[[:space:]]*$/ {n++; next} n>=2 {print}' "$file"
}

extract_description() {
    local file="$1"
    sed -n 's/^description:[[:space:]]*//p' "$file" | head -n1
}

to_codex_agent_skill() {
    local src="$1"
    local dst="$2"
    local name description

    name="$(basename "$src" .md)"
    description="$(extract_description "$src")"

    echo "sync agent: $src -> $dst"

    if [[ "$dry_run" -eq 1 ]]; then
        return 0
    fi

    mkdir -p "$(dirname "$dst")"
    {
        printf -- '---\n'
        printf 'name: %s\n' "$name"
        printf 'description: %s\n' "$description"
        printf -- '---\n\n'
        extract_body "$src" \
            | sed "s#~/.claude/#$repo_root/.claude/#g" \
            | sed 's/@code-investigator/`code-investigator`/g; s/@code-planner/`code-planner`/g; s/@code-safety-inspector/`code-safety-inspector`/g; s/@general-implementer/`general-implementer`/g; s/@web-api-implementer/`web-api-implementer`/g; s/@web-ui-implementer/`web-ui-implementer`/g'
    } > "$dst"
}

to_claude_agent() {
    local src="$1"
    local dst="$2"
    local name description frontmatter_tmp

    name="$(basename "$(dirname "$src")")"
    description="$(extract_description "$src")"
    frontmatter_tmp="$(mktemp)"

    echo "sync agent: $src -> $dst"

    if [[ "$dry_run" -eq 1 ]]; then
        rm -f "$frontmatter_tmp"
        return 0
    fi

    if [[ -f "$dst" ]]; then
        awk 'BEGIN{n=0} /^---[[:space:]]*$/ {n++; if (n==2) exit; next} n==1 {print}' "$dst" > "$frontmatter_tmp"
    else
        {
            printf 'name: %s\n' "$name"
            printf 'description: %s\n' "$description"
        } > "$frontmatter_tmp"
    fi

    if grep -q '^description:' "$frontmatter_tmp"; then
        sed -i'' -e "s#^description:.*#description: $description#" "$frontmatter_tmp"
    else
        printf 'description: %s\n' "$description" >> "$frontmatter_tmp"
    fi

    mkdir -p "$(dirname "$dst")"
    {
        printf -- '---\n'
        cat "$frontmatter_tmp"
        printf -- '---\n\n'
        extract_body "$src" \
            | sed "s#$repo_root/.claude/#~/.claude/#g" \
            | sed 's/`code-investigator`/@code-investigator/g; s/`code-planner`/@code-planner/g; s/`code-safety-inspector`/@code-safety-inspector/g; s/`general-implementer`/@general-implementer/g; s/`web-api-implementer`/@web-api-implementer/g; s/`web-ui-implementer`/@web-ui-implementer/g'
    } > "$dst"

    rm -f "$frontmatter_tmp"
}

sync_shared_skills_claude_to_codex() {
    local src_root=".claude/skills"
    local dst_root=".codex/skills"
    local skill

    for skill in "$src_root"/*; do
        [[ -d "$skill" ]] || continue

        case "$(basename "$skill")" in
            _tracker)
                continue
                ;;
        esac

        copy_dir "$skill" "$dst_root/$(basename "$skill")"
    done
}

sync_shared_skills_codex_to_claude() {
    local src_root=".codex/skills"
    local dst_root=".claude/skills"
    local skill name

    for skill in "$src_root"/*; do
        [[ -d "$skill" ]] || continue
        name="$(basename "$skill")"

        if [[ -d "$dst_root/$name" ]]; then
            copy_dir "$skill" "$dst_root/$name"
        fi
    done
}

sync_agents_claude_to_codex() {
    local src

    for src in .claude/agents/*.md; do
        [[ -f "$src" ]] || continue
        [[ "$(basename "$src")" == "INDEX.md" ]] && continue

        to_codex_agent_skill "$src" ".codex/skills/$(basename "$src" .md)/SKILL.md"
    done
}

sync_agents_codex_to_claude() {
    local src name

    for src in .codex/skills/*/SKILL.md; do
        [[ -f "$src" ]] || continue
        name="$(basename "$(dirname "$src")")"

        if [[ -f ".claude/agents/$name.md" ]]; then
            to_claude_agent "$src" ".claude/agents/$name.md"
        fi
    done
}

if [[ "$direction" == "claude" ]]; then
    sync_shared_skills_claude_to_codex
    sync_agents_claude_to_codex
else
    sync_shared_skills_codex_to_claude
    sync_agents_codex_to_claude
fi
