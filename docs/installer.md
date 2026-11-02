# The guided installer (issue #37)

`install.sh` is for people who have never used Linux: it asks questions in plain language (German
or English), explains why it needs each answer, and then does everything else. This page is for
the people who want to know what it does. The beginner steps are in the [README](../README.md); the
[AI-HELP.md](../AI-HELP.md) / [HILFE-MIT-KI.md](../HILFE-MIT-KI.md) files can be pasted into an AI
chat for step-by-step help.

## Using it

```bash
./install.sh                  # new installation, or a menu if one exists
./install.sh --update         # same as ./update.sh
./install.sh --repair         # run every step again and check
./install.sh --status         # steps done, services running
./install.sh --dry-run        # show every command and file write, change nothing (no sudo needed)
./install.sh --lang de|en     # skip the language question
./install.sh --answers FILE   # no questions (see below)
```

It runs only on **Debian 12 "bookworm"** and refuses anything else with a clear message (twice:
the bash wrapper and the Python program). It becomes root through `sudo` itself. The download
address for a lone `install.sh` is one constant, `REPO_URL` in `install.sh` (override with
`YTP_REPO_URL`).

## What is where

| Part | What it is |
|---|---|
| `install.sh`, `update.sh` | bash wrappers: system check, sudo, python3, then the program below |
| `scripts/install/install.py` | entry point |
| `scripts/install/ytp_install/` | the logic: `texts` (every sentence, de/en), `answers` (checks + the settings the answers become), `envfile` (safe `.env` writing), `system` (what it looks at), `runner` (runs commands), `steps` (the 8 steps), `update`, `cli` (the conversation) |
| `config/installer-state.json` | which steps are done, the chosen language. No secrets |
| `config/installer.log` | every command and its result, mode 600, never a password |
| `.env.bak-YYYYMMDD-HHMMSS` | a copy of `.env` before every change (mode 600, git-ignored, never deleted) |

## The steps

Each step uses the project's own script and is safe to run again.

| # | Step | What happens |
|---|---|---|
| 1 | Install Docker and the basic programs | `apt-get install ca-certificates curl git make`, then `scripts/host/bootstrap.sh` (Docker from Docker's official Debian repository, `/dev/net/tun`, the media user, the media folder) |
| 2 | Write the settings file .env | The template `.env.example` plus the answers; mode 600; a backup of an older file first. Settings new in the template are added with their defaults |
| 3 | Create and check the media folders | `scripts/host/create-layout.sh`, then `check-host.sh --only layout` (proves hardlinks work) |
| 4 | Prepare the VPN file | ExpressVPN-style `.ovpn`: copied to `config/expressvpn.ovpn` (mode 600) and `scripts/vpn/render_ovpn.py`; nothing for a built-in provider |
| 5 | Set up the logins (web page, qBittorrent, watchdog) | `scripts/proxy/set_password.py` (the web password becomes a bcrypt hash for qBittorrent's port) and `scripts/app/set_admin.py` (the same name and password become the app's first admin account, a scrypt hash, #44), `scripts/qbittorrent/seed_config.py`, `scripts/watchdog/setup_key.py`. All before the first start: qBittorrent rewrites its config on exit and gluetun reads its auth file only at start |
| 6 | Build and start the services, wait until everything runs | `docker compose up -d --build`, then waits (up to 10 minutes) until gluetun, qbittorrent, app, proxy and watchdog are all running and healthy |
| 7 | Set up qBittorrent (categories) and the Web UI settings the proxy needs (host header validation off, CSRF protection on, see [proxy.md](proxy.md)) | `scripts/qbittorrent/setup_categories.py` (an update runs it again, so an older installation gets the settings) |
| 8 | Check everything | `check-host.sh`, `verify_proxy.py` (asks the web password once more, Enter skips), Jellyfin's address, and optionally `killswitch-test.sh` |

### When the last step (checking everything) fails

At the end the installer checks the whole installation. If that step fails and a person is there
(no answers file), it does not just stop but offers a menu: **1 = Repair** (the default; steps 5 to
8 run again: logins, start, qBittorrent, checks; each is safe to repeat and changes only what is
not right, and the web password is not asked again), **2 = only check again**, **3 = quit**.
Starting `./install.sh` again after such a failure shows the same menu before it carries on, because
resuming alone would only repeat the checks and never the repair of an earlier step (for example
the qBittorrent setting that lets the proxy reach it). An installation by hand can run the
qBittorrent part alone: `scripts/qbittorrent/setup_categories.py --from-env` reads the user and
password from `.env`.

## Running it again

* **Stopped or failed:** the failing step is named with a plain hint and the log location; the next
  run reads the answers back from `.env`, skips the finished steps and continues. The web password is
  asked again only if its step was not finished (it is never stored).
* **Existing installation:** a menu. *Change settings* asks the questions again with the current
  values as defaults (Enter keeps a stored secret), writes `.env` (with a backup, unknown lines and
  your own additions kept) and runs the steps again except the Docker installation. *Update* is
  below. *Repair* runs every step again without questions. *State* lists steps and services.
  An installation made by hand before the installer existed gets the same menu.
* **Update** (`./update.sh`): backup first (`scripts/ops/backup.py`; no backup, no update), then
  `git pull --ff-only` as the folder's owner, new template settings added to `.env`, the VPN file
  rendered again, `docker compose up -d --build`, the wait for healthy, `check-host.sh`.
  An installation from before the accounts (#44) is asked once for an administrator password right
  after the settings are added (the web login's name is kept, `PROXY_USER`); the password is hashed
  with scrypt by `set_admin.py`. `check-host.sh --only login` and the page itself flag an
  installation that still has no admin (docs/accounts.md).

## The answers file (for automation and tests)

`--answers FILE` reads `key=value` lines and asks nothing. The keys are the fields of `Answers` in
`scripts/install/ytp_install/answers.py` (for example `vpn_kind=ovpn`, `ovpn_source=...`,
`vpn_user`, `vpn_password`, `web_password`, `data_root`, `lan_address`) plus the `MEDIA_DIR_*`
folder names. The file holds passwords, so it must be mode 600 (anything else is refused) and is
never echoed. Missing values are detected where possible (network, time zone); a missing
`qbit_password` is generated. Nothing is asked, so the optional killswitch test is skipped.

## Security

* **No secret on a command line.** Passwords reach the scripts through stdin (`--password-stdin`);
  the command runner refuses (as a bug) any command that contains a secret it knows, and the log
  never contains a command's input.
* **Hidden input** for every password and key; the summary shows `(set)` instead of secrets.
* **`.env`:** mode 600, written through a temporary file, values with special characters in single
  quotes so Docker Compose reads them back exactly. A single quote cannot be written that way, so a
  password containing `'` is refused with an explanation instead of silently breaking.
* **The web password is stored only as a bcrypt hash** made by Caddy; the qBittorrent password is
  generated (random) unless typed, kept in `.env` for the app and stored hashed (PBKDF2) by qBittorrent.
* The installer runs as root (it installs packages and creates users and folders) and hands `.env`
  back to the person who started it. Maintenance commands later need `sudo` (for example `sudo make backup`).

## Not covered

Formatting or mounting a separate media disk (the installer asks for the mount point; see
[host-setup.md](host-setup.md), step 4), several network cards, other Linux distributions, and
Jellyfin itself (create its libraries by hand: one per media folder).

## Tests

`tests/unit/test_installer_flow.py` plays whole conversations (new, German, WireGuard, wrong
answers, dry run, failure and resume, menu, change, repair, answers file, update) against a scripted
terminal and a recording runner and checks that no secret appears in anything shown, logged or run.
`test_installer_logic.py` covers the answer checks, `.env` handling, the texts (both languages
identical) and the system parsing; `test_install_sh.py` the bash wrapper; `test_installer_docs.py`
keeps the README, the two help files and the installer in step. The gated
`tests/system/test_c37_installer.py` (Docker, internet) runs `install.sh --dry-run` with an answers
file in a throwaway `debian:12` container.
