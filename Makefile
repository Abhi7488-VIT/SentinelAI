# SentinelAI developer workflow. Testbed targets delegate to testbed/tb.sh (also usable directly).
# Run from WSL (or any shell with docker + bash).

TB := ./testbed/tb.sh

.PHONY: help testbed-up testbed-ui testbed-down testbed-status testbed-logs testbed-test \
	deploy loadgen-stats loadgen-mix scenario-list scenario-validate scenario-status \
	scenario-run scenario-start scenario-stop

help:
	@echo "testbed-up                 build + start the testbed, wait until healthy"
	@echo "testbed-ui                 start Grafana (off by default) on 127.0.0.1:3001"
	@echo "testbed-down [CLEAN=1]     stop the testbed (CLEAN=1 also deletes its data)"
	@echo "testbed-status             show testbed containers"
	@echo "testbed-logs [SERVICE=x]   follow logs"
	@echo "testbed-test               testbed format, lint, typecheck, unit tests"
	@echo "deploy SERVICE=x VERSION=y [CONFIG='K=V ...']   deploy a release (emits deploy events)"
	@echo "loadgen-stats              load generator totals"
	@echo "loadgen-mix MIX=name       switch the traffic mix"
	@echo "scenario-list | scenario-validate | scenario-status"
	@echo "scenario-run ID=x [SEED=n] [BASELINE=s] [OBSERVE=s] [RECOVERY=s] [DRY_RUN=1]"
	@echo "                           full run: baseline, fault, checks, stop, recovery, verdict"
	@echo "scenario-start ID=x / scenario-stop ID=x   inject / remove a fault by hand (no verdict)"

testbed-up:
	@$(TB) up

testbed-ui:
	@$(TB) ui

testbed-down:
	@$(TB) down $(if $(CLEAN),--clean,)

testbed-status:
	@$(TB) status

testbed-logs:
	@$(TB) logs $(SERVICE)

testbed-test:
	@$(TB) test

deploy:
	@$(TB) deploy $(SERVICE) $(VERSION) $(CONFIG)

loadgen-stats:
	@$(TB) loadgen-stats

loadgen-mix:
	@$(TB) loadgen-mix $(MIX)

scenario-list:
	@$(TB) scenario list

scenario-validate:
	@$(TB) scenario validate

scenario-status:
	@$(TB) scenario status

scenario-run:
	@$(TB) scenario run $(ID) $(if $(SEED),--seed $(SEED),) $(if $(BASELINE),--baseline $(BASELINE),) \
		$(if $(OBSERVE),--observe $(OBSERVE),) $(if $(RECOVERY),--recovery $(RECOVERY),) \
		$(if $(DRY_RUN),--dry-run,)

scenario-start:
	@$(TB) scenario start $(ID) $(if $(SEED),--seed $(SEED),)

scenario-stop:
	@$(TB) scenario stop $(ID)
