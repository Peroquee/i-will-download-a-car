VENV ?= .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest

.PHONY: help venv lint test test-unit test-integration test-smoke test-system test-regression \
        smoke-host host-check snapshot clean

help:
	@echo "venv              create .venv with test and lint tools"
	@echo "lint              ruff, yamllint, shellcheck"
	@echo "test              all levels (skips what this machine can't do)"
	@echo "test-<level>      unit | integration | smoke | system | regression"
	@echo "host-check        run scripts/host/check-host.sh against ./.env (on the media VM)"
	@echo "smoke-host        smoke tests incl. full host checks (on the media VM)"
	@echo "snapshot          regenerate the compose regression snapshot after an intended change"

$(VENV)/.installed: requirements-test.txt requirements-lint.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt
	touch $@

venv: $(VENV)/.installed

lint: venv
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/yamllint --strict .
	find scripts -name '*.sh' -print0 | xargs -0 $(VENV)/bin/shellcheck --external-sources

test: venv
	$(PYTEST) tests

test-unit test-integration test-smoke test-system test-regression: test-%: venv
	$(PYTEST) tests/$*

host-check:
	scripts/host/check-host.sh --env-file .env

smoke-host: venv
	SMOKE_FULL_HOST=1 SMOKE_ENV_FILE=.env REQUIRE_CAPABILITIES=1 $(PYTEST) tests/smoke

snapshot: venv
	UPDATE_SNAPSHOTS=1 $(PYTEST) tests/regression -k snapshot

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache reports
