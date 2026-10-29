#!/bin/bash
set -e

AGENTSPATH="${AGENTSPATH:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

link_file() {
    local src="$1"
    local dst="$2"

    if [[ -L "$dst" ]]; then
        rm "$dst"
    elif [[ -e "$dst" ]]; then
        mv "$dst" "${dst}.backup"
        echo "  Backed up: $dst -> ${dst}.backup"
    fi

    ln -s "$src" "$dst"
    echo "  Linked: $dst -> $src"
}

# A skill is a directory that has SKILL.md directly under it.
is_skill_dir() {
    [[ -d "$1" && -f "$1/SKILL.md" ]]
}

# Make <dir> a real directory. A symlink is only unlinked: writing through it
# would create the skill links inside its target. The target is neither backed up nor modified.
ensure_real_dir() {
    local dir="$1"

    if [[ -L "$dir" ]]; then
        rm "$dir"
        echo "  Unlinked: $dir"
    elif [[ -e "$dir" && ! -d "$dir" ]]; then
        mv "$dir" "${dir}.backup"
        echo "  Backed up: $dir -> ${dir}.backup"
    fi

    mkdir -p "$dir"
}

# Remove symlinks directly under <dst_dir> that point into <src_dir> but no longer
# point at a skill directly under it. Real entries and links pointing outside
# <src_dir> are left untouched.
remove_stale_skill_links() {
    local src_dir="$1"
    local dst_dir="$2"
    local link target rest

    # Dot-named links are included (except . and ..). The extra patterns are used
    # instead of dotglob because saving it with `shopt -p` returns non-zero for a
    # disabled option and aborts under set -e. Unmatched patterns stay literal and
    # are filtered out by the -L test.
    for link in "$dst_dir"/* "$dst_dir"/.[!.]* "$dst_dir"/..?*; do
        [[ -L "$link" ]] || continue

        target="$(readlink "$link")"
        [[ "$target" == "$src_dir"/* ]] || continue

        rest="${target#"$src_dir"/}"
        if [[ -n "$rest" && "$rest" != */* && "$rest" != "." && "$rest" != ".." ]] \
            && is_skill_dir "$target"; then
            continue
        fi

        rm "$link"
        echo "  Removed stale link: $link -> $target"
    done
}

# Link each skill directly under <src_dir> to <dst_dir>/<name>.
deploy_skills() {
    local src_dir="$1"
    local dst_dir="$2"
    local skill_path

    if [[ ! -d "$src_dir" ]]; then
        echo "  Skipped: $src_dir does not exist"
        return 0
    fi

    remove_stale_skill_links "$src_dir" "$dst_dir"

    for skill_path in "$src_dir"/*; do
        is_skill_dir "$skill_path" || continue
        link_file "$skill_path" "$dst_dir/${skill_path##*/}"
    done
}

echo "==> Deploying Claude Code config..."

mkdir -p "$HOME/.claude"

# Files
# Claude Code reads only ~/.claude/CLAUDE.md at user scope, so the link keeps that name.
link_file "$AGENTSPATH/.claude/AGENTS.md"      "$HOME/.claude/CLAUDE.md"
link_file "$AGENTSPATH/.claude/settings.json"  "$HOME/.claude/settings.json"

# Directories
link_file "$AGENTSPATH/.claude/agents"  "$HOME/.claude/agents"
link_file "$AGENTSPATH/.claude/rules"   "$HOME/.claude/rules"

# Skills are linked one by one so that ~/.claude/skills can also hold
# local skills and runtime data that must not end up in the repository.
ensure_real_dir "$HOME/.claude/skills"
deploy_skills "$AGENTSPATH/.claude/skills" "$HOME/.claude/skills"

echo "==> Claude Code config deployed!"

echo "==> Deploying Codex skills..."

mkdir -p "$HOME/.codex/skills"

deploy_skills "$AGENTSPATH/.codex/skills" "$HOME/.codex/skills"

echo "==> Codex skills deployed!"

echo "==> Deploying local bin..."

mkdir -p "$HOME/.local/bin"

link_file "$AGENTSPATH/scripts/claude-rate-status" "$HOME/.local/bin/claude-rate-status"
link_file "$AGENTSPATH/scripts/codex-rate-status" "$HOME/.local/bin/codex-rate-status"
link_file "$AGENTSPATH/scripts/agent-rate-status" "$HOME/.local/bin/agent-rate-status"

echo "==> Local bin deployed!"
