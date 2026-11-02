# youtube-premium

Self-hosted web app on the home LAN that gets media into Jellyfin: paste a
YouTube link, a magnet link or a podcast feed, it downloads in the background (torrents only
through the VPN) and files the result into the right library.

## What is this? / Was ist das?

**English.** A small program for your home network. You open a web page, paste a link (a YouTube
video, a torrent "magnet" link or a podcast), and a few minutes later the film, episode, song or
podcast shows up in your Jellyfin media server, in the right folder, with a proper name. Torrents
are only ever downloaded through a VPN: if the VPN drops, they stop by themselves. Nothing is
reachable from the internet; the web page needs a login and answers only inside your home network.

**Deutsch.** Ein kleines Programm für dein Heimnetz. Du öffnest eine Webseite, fügst einen Link ein
(ein YouTube-Video, einen Torrent-„Magnet“-Link oder einen Podcast), und wenige Minuten später
erscheint der Film, die Folge, das Lied oder der Podcast in deinem Jellyfin-Medienserver, im
richtigen Ordner und mit ordentlichem Namen. Torrents werden nur über ein VPN geladen: fällt das
VPN aus, stoppen sie von selbst. Aus dem Internet ist nichts erreichbar; die Webseite braucht
eine Anmeldung und antwortet nur in deinem Heimnetz.

## Installation in 5 steps / Installation in 5 Schritten

No Linux knowledge needed. / Linux-Kenntnisse sind nicht nötig.

1. **A computer with Debian 12 / Ein Computer mit Debian 12.** A virtual machine (4 CPU cores,
   4 GB RAM, 32 GB disk) or a small PC, freshly installed with **Debian 12 "bookworm"**, with
   internet and a user that may use `sudo`. Creating the VM on Proxmox: [docs/host-setup.md](docs/host-setup.md).
   Other systems are refused by the installer.
2. **Your VPN details / Deine VPN-Zugangsdaten.** A VPN subscription. For ExpressVPN download the
   *OpenVPN* `.ovpn` file (account page → "Set up on more devices" → "Manual configuration") and
   copy it to the computer (WinSCP or `scp`); note the user name and password shown there (they are
   not your e-mail and password). Other providers: their name, user/password or WireGuard key.
3. **Download and start / Herunterladen und starten.** Copy these lines into the terminal of the
   Debian computer (it asks for your password once):

   ```bash
   sudo apt-get update && sudo apt-get install -y git   # makes sure git is there
   git clone https://github.com/Peroquee/i-will-download-a-car.git youtube-premium   # downloads the project
   cd youtube-premium && ./install.sh                   # starts the installer
   ```

4. **Answer the questions / Fragen beantworten.** The installer speaks German or English (it asks
   first), asks one question at a time and explains why it needs each answer. Press Enter to take
   the value in `[brackets]`. It asks for: the VPN details, where your media lives (default `/data`)
   and the folder names, a user name and password for the web page, your home network (it detects
   it), and optionally Jellyfin's address and key, a notification address and your internet line's
   IPv6 prefix. Then it installs Docker, creates the folders, builds and starts everything and
   checks it (15 to 30 minutes). Stopping with Ctrl+C is fine: running `./install.sh` again carries
   on where it stopped.
5. **Open the web page / Webseite öffnen.** At the end it prints the address, for example
   `http://192.168.178.20/`. Open it in a browser on any device in your home network, log in with
   the user and password from step 4, paste a link. In Jellyfin create one library per folder
   (e.g. `Filme`, `Serien`, `Musik`, `Podcasts`).

Later: `./install.sh` on an existing installation offers *change settings / update / repair /
state*; `./update.sh` updates (with a backup first). **Stuck?** Copy the file
[AI-HELP.md](AI-HELP.md) (English) or [HILFE-MIT-KI.md](HILFE-MIT-KI.md) (Deutsch) into an AI chat
assistant: it explains every step and every error message in plain language. Never send it
passwords, keys or the file `.env`. Details for experts: [docs/installer.md](docs/installer.md).

## Layout

```
docker-compose.yml     the stack (gluetun, qbittorrent, app, proxy, watchdog; Jellyfin is external)
proxy/                 the Caddyfile of the reverse proxy (LAN check + login)
app/                   the FastAPI app (package ytp) and its Dockerfile
compose/               per-VPN-type gluetun settings (vpn-openvpn.yml, vpn-wireguard.yml)
.env.example           every setting; copy to .env on the host
scripts/proxmox/       create the VM on the Proxmox node
scripts/host/          bootstrap, check and lay out folders on the Debian Docker host
scripts/qbittorrent/   seed qBittorrent's login and VPN binding; categories; netwatch (dead-namespace restart)
scripts/vpn/           render ExpressVPN's .ovpn for gluetun; the killswitch test
scripts/proxy/         set the proxy login; verify the proxy from the LAN
scripts/watchdog/      create the watchdog's read-only gluetun key
tests/<level>/         unit, integration, smoke, system, regression
docs/architecture.md   overview: services, flows, data, security model
docs/configuration.md  every setting (.env) and what it does
docs/host-setup.md     step-by-step host setup (issue #1)
docs/killswitch.md     how the VPN promise is proven: tests, expected output, failures
docs/app.md            the app: configuration, /health, database and migrations
docs/watchdog.md       the VPN watchdog: second layer that pauses torrents when the VPN drops
docs/proxy.md          reverse proxy and login: setup, design, what is tested
docs/jellyfin.md       the external Jellyfin: API key, libraries, audiobooks question
docs/testing.md        test levels, markers and rules
docs/operations.md     backups and restore, updates (pinned versions), log rotation
scripts/ops/           backup/restore and the yt-dlp update check
docs/youtube.md        YouTube probe and download, the VPN route, yt-dlp upkeep
docs/installer.md      the guided installer: what it does, re-running, updating, the answers file
install.sh, update.sh  the guided installer and updater (start here)
AI-HELP.md             paste into an AI chat for step-by-step help (English)
HILFE-MIT-KI.md        dasselbe auf Deutsch
docs/podcasts.md       podcasts: RSS feeds, episodes, the library, the SSRF-safe fetcher
docs/dashboard.md      the dashboard, the alert strip, where alerts come from
```

## Quick start

```bash
make venv && make test-unit      # no Docker needed
make test                        # everything this machine supports
```

## Reverse proxy and login (issue #29)

```bash
make proxy-password      # once: asks for the password, writes PROXY_USER / PROXY_PASSWORD_HASH to .env
docker compose up -d     # the proxy listens on PROXY_BIND:80 (app) and :8081 (qBittorrent)
make proxy-check         # from a LAN machine: 401 without login, 200 with it, backend ports closed
```

The app and qBittorrent's Web UI are reachable only through Caddy, only from `LAN_SUBNET`, only
with the login. Design and troubleshooting: [docs/proxy.md](docs/proxy.md).

## Killswitch test (on the media VM, stack running)

```bash
make up                  # renders config/expressvpn.ovpn for gluetun, starts the stack
make killswitch-test     # stops gluetun, checks qbittorrent has no network, then recovery
```

Passes when the output ends with `OK: the killswitch holds and qbittorrent recovers`:
qBittorrent exits through a VPN address (`VPN IP`), has `no connection` while gluetun is
stopped (never a `HOME IP LEAK`), and gets its connection back automatically (netwatch restarts the container) after gluetun
is started again. Only verdicts are printed, never an address. Details, and what to do when
it fails: [docs/killswitch.md](docs/killswitch.md).

Host setup: [docs/host-setup.md](docs/host-setup.md). Testing: [docs/testing.md](docs/testing.md).

## Ground rules

No traffic outside the VPN · LAN only, behind a login · never fail silently · recover cleanly.
