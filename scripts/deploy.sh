#!/bin/bash
set -e

AGENTSPATH="${AGENTSPATH:-$HOME/workspace/agents}"

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

echo "==> Deploying Claude Code config..."

mkdir -p "$HOME/.claude"

# Files
link_file "$AGENTSPATH/.claude/CLAUDE.md"      "$HOME/.claude/CLAUDE.md"
link_file "$AGENTSPATH/.claude/settings.json"  "$HOME/.claude/settings.json"

# Directories
link_file "$AGENTSPATH/.claude/agents"  "$HOME/.claude/agents"
link_file "$AGENTSPATH/.claude/skills"  "$HOME/.claude/skills"
link_file "$AGENTSPATH/.claude/rules"   "$HOME/.claude/rules"

echo "==> Claude Code config deployed!"

echo "==> Deploying Codex skills..."

mkdir -p "$HOME/.codex/skills"

if [[ -d "$AGENTSPATH/.codex/skills" ]]; then
    for skill_path in "$AGENTSPATH"/.codex/skills/*; do
        [[ -e "$skill_path" ]] || continue

        skill_name="$(basename "$skill_path")"
        link_file "$skill_path" "$HOME/.codex/skills/$skill_name"
    done
else
    echo "  Skipped: $AGENTSPATH/.codex/skills does not exist"
fi

echo "==> Codex skills deployed!"
