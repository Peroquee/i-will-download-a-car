VENV ?= .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest

.PHONY: help venv lint test test-unit test-integration test-smoke test-system test-regression \
        smoke-host host-check snapshot clean vpn-config up killswitch-test layout categories proxy-password proxy-check watchdog-key vpn-abuse-test backup backup-list update-check recovery-prepare recovery-verify install update

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
	@echo "layout            on the media VM: create missing download/library folders and config/app (owned by PUID:PGID), prove hardlinks"
	@echo "categories        set up the qBittorrent categories and save paths (asks for the Web UI password)"
	@echo "proxy-password  set the proxy login in .env (asks for the password, hashes it with Caddy)"
	@echo "proxy-check     from a LAN machine: 401 without login, 200 with it, backend ports closed"
	@echo "watchdog-key    create the VPN watchdog's read-only gluetun key (.env and gluetun's auth file)"
	@echo "vpn-abuse-test  on the VM: drop the VPN CYCLES times (default 5), check the watchdog, leaks and recovery"
	@echo "backup           back up the database, config/ and .env to <DATA_ROOT>/.youtube-premium-backups (mode 600; DRY=1 to only show)"
	@echo "backup-list      list the backups"
	@echo "update-check     is the pinned yt-dlp the newest release? (exit 10 = newer exists); lists the pinned image tags"
	@echo "recovery-prepare  before a reboot of the VM or the Proxmox host: record what runs and which jobs are in progress"
	@echo "recovery-verify   after the reboot: all containers back, in order, VPN before traffic, jobs recovered or failed cleanly"
	@echo "install          guided installation for beginners (asks questions in German or English; sudo)"
	@echo "update           back up, fetch the new version, rebuild, check"
	@echo "snapshot          regenerate the compose regression snapshot after an intended change"

# Every file requirements-dev.txt pulls in (with -r, also nested) must be listed, or a venv made before
# a dependency was added keeps missing it. tests/unit/test_makefile.py checks that.
$(VENV)/.installed: requirements-dev.txt requirements-test.txt requirements-lint.txt requirements-app.txt app/requirements.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt
	touch $@

venv: $(VENV)/.installed

lint: venv
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/yamllint --strict .
	find scripts -type f \( -name '*.sh' -o -path '*/services/*' \) -print0 | xargs -0 $(VENV)/bin/shellcheck --external-sources
	$(VENV)/bin/shellcheck --external-sources install.sh update.sh

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

watchdog-key:
	scripts/watchdog/setup_key.py

CYCLES ?= 5
vpn-abuse-test:
	scripts/vpn/vpn-abuse-test.sh --cycles $(CYCLES)

DRY ?=
backup:
	scripts/ops/backup.py create $(if $(DRY),--dry-run,)

backup-list:
	scripts/ops/backup.py list

update-check:
	-scripts/ops/update_ytdlp.py
	@echo "image tags in use:"
	@grep -hE '^[A-Z]+_TAG=' $$(test -f .env && echo .env || echo .env.example) | sed 's/^/  /'

recovery-prepare:
	scripts/ops/recovery_test.py prepare

recovery-verify:
	scripts/ops/recovery_test.py verify

install:
	./install.sh

update:
	./update.sh

snapshot: venv
	UPDATE_SNAPSHOTS=1 $(PYTEST) tests/regression -k snapshot

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache reports
