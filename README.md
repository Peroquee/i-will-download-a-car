# youtube-premium

Self-hosted web app on the home LAN that gets media into Jellyfin: paste a
YouTube link or a magnet, it downloads in the background (torrents only through
the VPN) and files the result into the right library.

## Layout

```
docker-compose.yml     the stack (gluetun, qbittorrent, app; watchdog/proxy planned; Jellyfin is external)
app/                   the FastAPI app (package ytp) and its Dockerfile
compose/               per-VPN-type gluetun settings (vpn-openvpn.yml, vpn-wireguard.yml)
.env.example           every setting; copy to .env on the host
scripts/proxmox/       create the VM on the Proxmox node
scripts/host/          bootstrap, check and lay out folders on the Debian Docker host
scripts/qbittorrent/   seed qBittorrent's login and VPN binding; categories; netwatch (dead-namespace restart)
scripts/vpn/           render ExpressVPN's .ovpn for gluetun; the killswitch test
tests/<level>/         unit, integration, smoke, system, regression
docs/host-setup.md     step-by-step host setup (issue #1)
docs/jellyfin.md       the external Jellyfin: API key, libraries, audiobooks question
docs/testing.md        test levels, markers and rules
index.html             first draft of the submission form (moves into app/ with #8)
```

## Quick start

```bash
make venv && make test-unit      # no Docker needed
make test                        # everything this machine supports
```

## Killswitch test (on the media VM, stack running)

```bash
make up                  # renders config/expressvpn.ovpn for gluetun, starts the stack
make killswitch-test     # stops gluetun, checks qbittorrent has no network, then recovery
```

Passes when the output ends with `OK: the killswitch holds and qbittorrent recovers`:
qBittorrent exits through a VPN address (`VPN IP`), has `no connection` while gluetun is
stopped (never a `HOME IP LEAK`), and gets its connection back automatically (netwatch restarts the container) after gluetun
is started again. Only verdicts are printed, never an address. Details, and what to do when
it fails: [docs/host-setup.md](docs/host-setup.md#5c-prove-the-killswitch-issue-4).

Host setup: [docs/host-setup.md](docs/host-setup.md). Testing: [docs/testing.md](docs/testing.md).

## Ground rules

No traffic outside the VPN · LAN only, behind a login · never fail silently · recover cleanly.
