VENV ?= .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest

.PHONY: help venv lint test test-unit test-integration test-smoke test-system test-regression \
        smoke-host host-check snapshot clean vpn-config up killswitch-test layout categories proxy-password proxy-check

help:
	@echo "venv              create .venv with test and lint tools"
	@echo "lint              ruff, yamllint, shellcheck"
	@echo "test              all levels (skips what this machine can't do)"
	@echo "test-<level>      unit | integration | smoke | system | regression"
	@echo "host-check        run scripts/host/check-host.sh against ./.env (on the media VM)"
	@echo "smoke-host        smoke tests incl. full host checks (on the media VM)"
	@echo "vpn-config        render config/expressvpn.ovpn for gluetun (resolves the server name to an IP)"
	@echo "up                vpn-config, then docker compose up -d"
	@echo "killswitch-test   on the media VM: stop gluetun, prove qbittorrent has no network, check recovery"
	@echo "layout            on the media VM: create missing download/library folders and prove hardlinks"
	@echo "categories        set up the qBittorrent categories and save paths (asks for the Web UI password)"
	@echo "proxy-password  set the proxy login in .env (asks for the password, hashes it with Caddy)"
	@echo "proxy-check     from a LAN machine: 401 without login, 200 with it, backend ports closed"
	@echo "snapshot          regenerate the compose regression snapshot after an intended change"

$(VENV)/.installed: requirements-test.txt requirements-lint.txt requirements-app.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt
	touch $@

venv: $(VENV)/.installed

lint: venv
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/yamllint --strict .
	find scripts -type f \( -name '*.sh' -o -path '*/services/*' \) -print0 | xargs -0 $(VENV)/bin/shellcheck --external-sources

test: venv
	$(PYTEST) tests

test-unit test-integration test-smoke test-system test-regression: test-%: venv
	$(PYTEST) tests/$*

host-check:
	scripts/host/check-host.sh --env-file .env

smoke-host: venv
	SMOKE_FULL_HOST=1 SMOKE_ENV_FILE=.env REQUIRE_CAPABILITIES=1 $(PYTEST) tests/smoke

vpn-config:
	scripts/vpn/render_ovpn.py

up: vpn-config
	docker compose up -d

killswitch-test:
	scripts/vpn/killswitch-test.sh

layout:
	sudo scripts/host/create-layout.sh
	sudo scripts/host/check-host.sh --only layout

categories:
	scripts/qbittorrent/setup_categories.py

proxy-password:
	scripts/proxy/set_password.py

proxy-check:
	scripts/proxy/verify_proxy.py

snapshot: venv
	UPDATE_SNAPSHOTS=1 $(PYTEST) tests/regression -k snapshot

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache reports
