# youtube-premium

Self-hosted web app on the home LAN that gets media into Jellyfin: paste a
YouTube link or a magnet, it downloads in the background (torrents only through
the VPN) and files the result into the right library.

## Layout

```
docker-compose.yml     the stack (gluetun, qbittorrent; app/watchdog/proxy planned)
compose/               per-VPN-type gluetun settings (vpn-openvpn.yml, vpn-wireguard.yml)
.env.example           every setting; copy to .env on the host
scripts/proxmox/       create the VM on the Proxmox node
scripts/host/          bootstrap and check the Debian Docker host
scripts/qbittorrent/   seed qBittorrent's Web UI login and VPN binding
tests/<level>/         unit, integration, smoke, system, regression
docs/host-setup.md     step-by-step host setup (issue #1)
docs/testing.md        test levels, markers and rules
index.html             first draft of the submission form (moves into app/ with #8)
```

## Quick start

```bash
make venv && make test-unit      # no Docker needed
make test                        # everything this machine supports
```

Host setup: [docs/host-setup.md](docs/host-setup.md). Testing: [docs/testing.md](docs/testing.md).

## Ground rules

No traffic outside the VPN · LAN only, behind a login · never fail silently · recover cleanly.
