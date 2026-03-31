AGENTSPATH := $(realpath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: deploy update sync-skills-claude sync-skills-codex sync-skills-claude-dry sync-skills-codex-dry help

deploy:
	@AGENTSPATH=$(AGENTSPATH) bash $(AGENTSPATH)/scripts/deploy.sh

update:
	git pull origin master
	@AGENTSPATH=$(AGENTSPATH) bash $(AGENTSPATH)/scripts/deploy.sh

sync-skills-claude:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from claude

sync-skills-codex:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from codex

sync-skills-claude-dry:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from claude --dry-run

sync-skills-codex-dry:
	@bash $(AGENTSPATH)/scripts/sync-claude-codex-skills.sh --from codex --dry-run

help:
	@echo "Usage:"
	@echo "  make deploy  - Deploy Claude Code config (CLAUDE.md, agents, skills, rules)"
	@echo "  make update  - Pull latest and redeploy"
	@echo "  make sync-skills-claude      - Sync Claude skills/agents into Codex"
	@echo "  make sync-skills-codex       - Sync Codex skills into Claude"
	@echo "  make sync-skills-claude-dry  - Preview Claude -> Codex sync"
	@echo "  make sync-skills-codex-dry   - Preview Codex -> Claude sync"
