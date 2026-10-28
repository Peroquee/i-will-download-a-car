# Testing strategy

Every feature ships with tests at each level that applies to it. A test's level
is its folder; `tests/conftest.py` tags it automatically and refuses tests
placed anywhere else.

| Level | Folder | Question it answers | Needs | Runs on |
|---|---|---|---|---|
| unit | `tests/unit/` | Does this piece of logic or config do what it says, in isolation? | nothing (stubs, fake dirs) | any runner, every push |
| integration | `tests/integration/` | Do two real components agree (our files + Compose CLI; later app + SQLite, app + qBittorrent API)? | the component under test | any runner (`docker compose` CLI only so far) |
| smoke | `tests/smoke/` | Is this host or stack alive at all? Fast gate before slower tests. | Docker daemon | `linux` runner; `make smoke-host` on the media VM |
| system | `tests/system/` | Does the real thing behave correctly end to end, black-box? | Docker daemon, `/dev/net/tun` | `linux` runner |
| regression | `tests/regression/` | Has a fixed bug or a deliberate decision come back? | varies | `linux` runner |

Cross-cutting markers, combinable with any level:

- `security`: guards a ground rule (VPN-only traffic, LAN-only, no secrets in git). `pytest -m security` must always pass.
- `compose`, `docker`, `tun`, `network`: required capabilities. Missing ones **skip with a reason** locally and **fail** in CI (`REQUIRE_CAPABILITIES=1`), so a broken runner can't produce a green pipeline.
- `slow`: more than ~10 s.

## Rules

1. **Per feature, per issue.** An issue is done when its acceptance criteria are covered by tests: unit for logic and config shape, integration for every boundary it touches, system for its observable behaviour, smoke if it adds something that must be alive, and a regression test for every bug found while building it.
2. **Negative tests are mandatory for safety features.** Show the guard works *and* that removing it breaks things (example: tun works with `NET_ADMIN`, is denied without it).
3. **Regression tests are named `test_<issue>_<what>`** (e.g. `test_c0_qbittorrent_has_no_network_of_its_own`), say in the docstring which bug or decision they pin, and are never deleted. Changing one needs its own issue.
4. **Unit tests never touch the real system.** Shell scripts are tested through stub commands on `PATH` (`tests/helpers/shell.py`) and test hooks like `CHECK_ROOT`, `OS_RELEASE`, `FSTAB_FILE`, `--dry-run`.
5. **System tests clean up after themselves.** Every container/volume is named `ytp-test-*` (`tests/helpers/docker.unique_name`) and removed in a fixture; CI's `after_script` sweeps leftovers.
6. **No test depends on the shell environment.** Compose tests run with variables the stack uses stripped from the environment (`tests/helpers/compose_cli.clean_env`).
7. **Snapshots change deliberately.** `make snapshot` regenerates `tests/regression/snapshots/`; the diff is reviewed in the MR.

## Running

```bash
make venv
make test-unit          # seconds, no Docker needed
make test               # everything this machine can do; the rest is skipped with reasons
make smoke-host         # on the media VM: full host checks against the real .env
```

Images used by system tests can point at a mirror:
`TEST_IMAGE_BUSYBOX=registry.lan/busybox:1.37 TEST_IMAGE_PYTHON=registry.lan/python:3.12-alpine`.

## Planned additions as the app grows

- `app/` code gets unit tests with coverage reported in `test:unit` (`--cov=app`).
- Contract tests for the qBittorrent and Jellyfin API wrappers against recorded responses (integration).
- Killswitch tests (#4, #21) live in `tests/system/` with `security` and run against a real gluetun.
- UI tests for the HTMX views (#24 to #29) in `tests/system/` with a headless browser.
