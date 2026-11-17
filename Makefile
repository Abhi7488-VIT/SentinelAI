# SentinelAI developer workflow. Testbed targets delegate to testbed/tb.sh (also usable directly).
# Run from WSL (or any shell with docker + bash).

TB := ./testbed/tb.sh

.PHONY: help testbed-up testbed-down testbed-status testbed-logs testbed-test

help:
	@echo "testbed-up                 build + start the testbed, wait until healthy"
	@echo "testbed-down [CLEAN=1]     stop the testbed (CLEAN=1 also deletes its data)"
	@echo "testbed-status             show testbed containers"
	@echo "testbed-logs [SERVICE=x]   follow logs"
	@echo "testbed-test               testbed format, lint, typecheck, unit tests"

testbed-up:
	@$(TB) up

testbed-down:
	@$(TB) down $(if $(CLEAN),--clean,)

testbed-status:
	@$(TB) status

testbed-logs:
	@$(TB) logs $(SERVICE)

testbed-test:
	@$(TB) test
