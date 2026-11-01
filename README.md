# youtube-premium

Self-hosted web app on the home LAN that gets media into Jellyfin: paste a
YouTube link or a magnet, it downloads in the background (torrents only through
the VPN) and files the result into the right library.

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
docs/podcasts.md       podcasts: RSS feeds, episodes, the library, the SSRF-safe fetcher
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
