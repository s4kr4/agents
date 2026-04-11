AGENTSPATH := $(realpath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: deploy update sync-skills-claude sync-skills-codex sync-skills-claude-dry sync-skills-codex-dry memory-init memory-demo help

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

memory-init:
	@python3 $(AGENTSPATH)/memory/memory.py init-db

memory-demo:
	@python3 $(AGENTSPATH)/memory/memory.py init-db
	@python3 $(AGENTSPATH)/memory/memory.py start-session --client codex --user-id default --project-id agents --session-id demo
	@python3 $(AGENTSPATH)/memory/memory.py append-event --session-id demo --client codex --user-id default --project-id agents --role user --kind message --content '応答は日本語で行ってください'
	@python3 $(AGENTSPATH)/memory/memory.py extract --session-id demo
	@python3 $(AGENTSPATH)/memory/memory.py consolidate --entity-id default
	@python3 $(AGENTSPATH)/memory/memory.py get-context --user-id default --project-id agents
	@python3 $(AGENTSPATH)/memory/memory.py end-session --session-id demo --append-summary-event

help:
	@echo "Usage:"
	@echo "  make deploy  - Deploy Claude Code config (CLAUDE.md, agents, skills, rules)"
	@echo "  make update  - Pull latest and redeploy"
	@echo "  make sync-skills-claude      - Sync Claude skills/agents into Codex"
	@echo "  make sync-skills-codex       - Sync Codex skills into Claude"
	@echo "  make sync-skills-claude-dry  - Preview Claude -> Codex sync"
	@echo "  make sync-skills-codex-dry   - Preview Codex -> Claude sync"
	@echo "  make memory-init             - Initialize the shared memory SQLite DB"
	@echo "  make memory-demo             - Run a minimal shared memory demo"
