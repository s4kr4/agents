AGENTSPATH := $(realpath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: deploy update help

deploy:
	@AGENTSPATH=$(AGENTSPATH) bash $(AGENTSPATH)/scripts/deploy.sh

update:
	git pull origin master
	@AGENTSPATH=$(AGENTSPATH) bash $(AGENTSPATH)/scripts/deploy.sh

help:
	@echo "Usage:"
	@echo "  make deploy  - Deploy Claude Code config (CLAUDE.md, agents, skills, rules)"
	@echo "  make update  - Pull latest and redeploy"
