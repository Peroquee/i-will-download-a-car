# Testing strategy

Every feature ships with tests at each level that applies to it. A test's level
is its folder; `tests/conftest.py` tags it automatically and refuses tests
placed anywhere else.

| Level | Folder | Question it answers | Needs | Runs on |
|---|---|---|---|---|
| unit | `tests/unit/` | Does this piece of logic or config do what it says, in isolation? | nothing (stubs, fake dirs) | any runner, every push |
| integration | `tests/integration/` | Do two real components agree (our files + Compose CLI; later app + SQLite, app + qBittorrent API)? | the component under test | any runner (`docker compose` CLI only so far) |
| smoke | `tests/smoke/` | Is this host or stack alive at all? Fast gate before slower tests. | Docker daemon | `ytp-host` runner (gated, see below); `make smoke-host` on the media VM |
| system | `tests/system/` | Does the real thing behave correctly end to end, black-box? | Docker daemon, `/dev/net/tun` | `ytp-host` runner (gated) |
| regression | `tests/regression/` | Has a fixed bug or a deliberate decision come back? | varies | `ytp-host` runner (gated) |

Cross-cutting markers, combinable with any level:

- `security`: guards a ground rule (VPN-only traffic, LAN-only, no secrets in git). `pytest -m security` must always pass.
- Tests that build real git repositories (`test_autodeploy.py`) need `git`: the `test:unit` job installs it (Debian's 2.39 series) and the tests **fail** in CI if it is missing; locally they skip with a loud reason.
- `compose`, `docker`, `tun`, `network`: required capabilities. Missing ones **skip with a reason** locally and **fail** in CI (`REQUIRE_CAPABILITIES=1`), so a broken runner can't produce a green pipeline.
- `slow`: more than ~10 s.

### The app (C3)

`app/` is a FastAPI package (`ytp`) with its own Dockerfile. Its **unit tests** live in
`tests/unit/test_app_*.py` like all others and run in the normal `test:unit` job, which also
installs `requirements-app.txt` (the app's pinned dependencies plus the test client). CI has
no Docker daemon, so the **image build** cannot be checked there: `tests/unit/test_app_image.py`
pins what the build and compose rely on (pinned base, health check, non-root, no published
port), and `tests/system/test_c3_app_image.py` builds the real image, runs it as an arbitrary
uid and waits for Docker's health status (gated like the other host tests; on the VM:
`make test-system`, or by hand `docker compose build app && docker compose up -d app &&
docker compose ps`, where the app must show `healthy`).

### Tests that need real credentials (C1)

`tests/system/test_c1_qbittorrent_login.py` starts a real qBittorrent with a seeded login
(needs Docker and the image, no VPN). `tests/system/test_c1_tunnel.py` starts the real
stack and checks that gluetun is healthy and qBittorrent leaves through the tunnel; it
needs ExpressVPN credentials and runs only with `YTP_VPN_ENV_FILE=/path/to/.env`, e.g.
on the media VM: `YTP_VPN_ENV_FILE=.env make test-system`. Without that variable it
skips, even under `REQUIRE_CAPABILITIES`. It uses a throwaway project name and data
folder; it does not touch the running stack or the media volume.

## Rules

1. **Per feature, per issue.** An issue is done when its acceptance criteria are covered by tests: unit for logic and config shape, integration for every boundary it touches, system for its observable behaviour, smoke if it adds something that must be alive, and a regression test for every bug found while building it.
2. **Negative tests are mandatory for safety features.** Show the guard works *and* that removing it breaks things (example: tun works with `NET_ADMIN`, is denied without it).
3. **Regression tests are named `test_<issue>_<what>`** (e.g. `test_c0_qbittorrent_has_no_network_of_its_own`), say in the docstring which bug or decision they pin, and are never deleted. Changing one needs its own issue.
4. **Unit tests never touch the real system.** Shell scripts are tested through stub commands on `PATH` (`tests/helpers/shell.py`) and test hooks like `CHECK_ROOT`, `OS_RELEASE`, `FSTAB_FILE`, `--dry-run`.
5. **System tests clean up after themselves.** Every container/volume is named `ytp-test-*` (`tests/helpers/docker.unique_name`) and removed in a fixture; CI's `after_script` sweeps leftovers.
6. **No test depends on the shell environment.** Compose tests run with variables the stack uses stripped from the environment (`tests/helpers/compose_cli.clean_env`).
7. **Snapshots change deliberately.** `make snapshot` regenerates `tests/regression/snapshots/`; the diff is reviewed in the MR.

## Where the tests run

- **Lint, unit and integration** need no Docker daemon and run on the shared docker-executor runner.
- **Smoke, system and regression** need the real youtube-premium host (Docker daemon, `/dev/net/tun`). They carry the tag `ytp-host` and run only on a dedicated runner on that VM. They are deliberately **not** run on the shared runner: it would need `/var/run/docker.sock` mounted or privileged dind, which gives any job root on a host that also builds other projects.
- **The `ytp-host` runner does not exist yet** (the VM is not provisioned). Until it does, these three jobs are gated by the CI/CD variable `YTP_HOST_RUNNER`: unless it is `"1"` they are absent from the pipeline. They do not block merge requests and they do not report a pass they did not earn. Run them by hand on the VM with `make smoke-host` / `make test`.
- **To enable them:** register a runner on the VM with the tag `ytp-host` (docker executor with the host socket mounted, or shell executor; see the header of `.gitlab-ci.yml`), then set `YTP_HOST_RUNNER=1` under Settings > CI/CD > Variables. From then on a missing daemon or tun device fails the job (`REQUIRE_CAPABILITIES=1`).

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
