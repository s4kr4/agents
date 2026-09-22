AGENTSPATH := $(realpath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: deploy update sync-skills-claude sync-skills-codex sync-skills-claude-dry sync-skills-codex-dry hooks-install hooks-uninstall help

deploy:
	@AGENTSPATH=$(AGENTSPATH) bash $(AGENTSPATH)/scripts/deploy.sh

update:
	git pull origin main
	@AGENTSPATH=$(AGENTSPATH) bash $(AGENTSPATH)/scripts/deploy.sh

sync-skills-claude:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from claude

sync-skills-codex:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from codex

sync-skills-claude-dry:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from claude --dry-run

sync-skills-codex-dry:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from codex --dry-run

hooks-install:
	@git -C $(AGENTSPATH) config core.hooksPath .githooks
	@echo "Enabled repo hooks (core.hooksPath=.githooks)"

hooks-uninstall:
	@git -C $(AGENTSPATH) config --unset core.hooksPath 2>/dev/null || true
	@echo "Disabled repo hooks (core.hooksPath unset)"

help:
	@echo "Usage:"
	@echo "  make deploy  - Deploy Claude Code config (CLAUDE.md, agents, skills, rules)"
	@echo "  make update  - Pull latest and redeploy"
	@echo "  make sync-skills-claude      - Sync Claude skills/agents into Codex"
	@echo "  make sync-skills-codex       - Sync Codex skills into Claude"
	@echo "  make sync-skills-claude-dry  - Preview Claude -> Codex sync"
	@echo "  make sync-skills-codex-dry   - Preview Codex -> Claude sync"
	@echo "  make hooks-install           - Enable the repo pre-commit hook (skill sync check)"
	@echo "  make hooks-uninstall         - Disable the repo pre-commit hook"
