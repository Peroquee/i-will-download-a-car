# Configuration reference (issue #31)

All settings live in **`.env`** next to `docker-compose.yml` (`cp .env.example .env && chmod 600
.env`, never commit it). `.env.example` is the template with working defaults; a unit test fails if
a variable used by the compose file is missing from it, and another fails if a variable below is
missing from this page. After changing `.env`: `docker compose up -d` (it recreates only the
services whose settings changed).

**Required** means compose refuses to start without a value. A value of `changeme` is a placeholder
that deliberately matches nothing. **Secret** means: only in `.env` on the VM, never in Git, never
in a log.

## General and ownership

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TZ` | `Europe/Berlin` | yes | Time zone of every container (log times, schedules). |
| `LOG_LEVEL` | `INFO` | no | `DEBUG`, `INFO`, `WARNING` or `ERROR`; app and watchdog. |
| `PUID`, `PGID` | `1000` | yes | User and group that own `/data`; the app, watchdog, proxy and qBittorrent run as them. Must match the media volume's owner and the user Jellyfin reads as. |
| `UMASK` | `002` | yes | New files 664, folders 775: group-writable, so the app, qBittorrent and Jellyfin share files. |

## Storage

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `DATA_ROOT` | `/data` | yes | Mount point of the media volume. `downloads/` and all library folders must be on **one filesystem** so hardlinks work ([host-setup.md](host-setup.md)). |
| `MEDIA_DIR_MOVIES`, `MEDIA_DIR_SERIES`, `MEDIA_DIR_AUDIOBOOKS`, `MEDIA_DIR_MUSIC`, `MEDIA_DIR_PODCASTS`, `MEDIA_DIR_YOUTUBE` | `Filme`, `Serien`, `Hörbücher`, `Musik`, `Podcasts`, `Youtube` | yes | Library folder per type, relative to `DATA_ROOT`: the folders Jellyfin's libraries point at. Must be distinct and match the folders on disk exactly (umlauts included). Downloads go to `downloads/<type>` first and are hardlinked here when finished. |

## Network and the login

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `LAN_SUBNET` | `192.168.178.0/24` | yes | Home LAN in CIDR form. gluetun's firewall lets it through (so the UI answers) and the proxy refuses clients outside it. |
| `QBIT_WEBUI_BIND` | `127.0.0.1` | yes | Where gluetun publishes qBittorrent's Web UI port. Keep `127.0.0.1`: the LAN uses the proxy, this port is for maintenance scripts on the VM. |
| `QBIT_WEBUI_PORT` | `8080` | yes | That port. |
| `PROXY_BIND` | `192.168.178.123` | yes | The **VM's own LAN address** (`hostname -I`). The only address the proxy listens on; compose refuses to start the proxy if it is not on this machine. Do not use `0.0.0.0`. |
| `PROXY_PORT` | `80` | yes | The app is at `http://<VM>:<PROXY_PORT>/`. |
| `PROXY_QBT_PORT` | `8081` | yes | qBittorrent's Web UI through the proxy. |
| `PROXY_USER` | `admin` | yes | Login name. |
| `PROXY_PASSWORD_HASH` | `changeme` | yes, **secret** | bcrypt hash of the password. Do not edit by hand: `make proxy-password` writes both lines. Until then the proxy refuses everyone. [proxy.md](proxy.md) |

## VPN (gluetun)

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `VPN_SERVICE_PROVIDER` | `custom` | yes | `custom` uses ExpressVPN's own `.ovpn` file (the built-in provider is broken in the pinned gluetun, see `.env.example`). |
| `VPN_TYPE` | `openvpn` | no | `openvpn` or `wireguard`. ExpressVPN supports OpenVPN only. Only the variables of the chosen type are read. |
| `OPENVPN_CUSTOM_CONFIG` | `/gluetun/custom/expressvpn.ovpn` | no | Path (inside the container) of the rendered `.ovpn`; empty with a built-in provider. `make vpn-config` renders it. |
| `OPENVPN_USER`, `OPENVPN_PASSWORD` | `changeme` | with `openvpn`, **secret** | ExpressVPN's manual-configuration credentials (not the account e-mail and password). |
| `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES` | `changeme`, `10.0.0.2/32` | with `wireguard`, **secret** | Only for a provider that offers WireGuard. |
| `VPN_SERVER_COUNTRIES`, `VPN_SERVER_CITIES` | empty | no | Comma-separated, only for a built-in provider. Must stay empty with `custom`. |

ExpressVPN has no port forwarding: do not add `VPN_PORT_FORWARDING`.

## Jellyfin and qBittorrent (for the app)

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `JELLYFIN_URL` | `http://192.168.178.108:8096` | no | The external Jellyfin. Empty = no library refreshes. [jellyfin.md](jellyfin.md) |
| `JELLYFIN_API_KEY` | empty | no, **secret** | Dashboard > API Keys. Empty = refreshes off (the app says so at start). |
| `QBIT_USER` | `admin` | no | qBittorrent Web UI user the app and watchdog log in as. |
| `QBIT_PASSWORD` | empty | no, **secret** | Its password (chosen with `scripts/qbittorrent/seed_config.py`). Empty = the app starts, downloads are disabled and it says so. The watchdog also needs it. |

## VPN watchdog and alerts ([watchdog.md](watchdog.md))

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `GLUETUN_CONTROL_APIKEY` | `changeme` | yes, **secret** | The watchdog's read-only gluetun key; `make watchdog-key` creates it and gluetun's auth file. |
| `WATCHDOG_INTERVAL` | `1` | no | Seconds between polls of the VPN status (0.2 to 60). |
| `WATCHDOG_THRESHOLD` | `3` | no | Failed polls in a row before torrents are paused (1 to 60). |
| `WATCHDOG_RESUME_AFTER` | `0` | no | Seconds the VPN must stay up before paused torrents are resumed; `0` = never automatically, you resume them. |
| `ALERT_URL` | empty | no, **secret** | Where alerts go (an ntfy topic URL or a webhook). **Empty = no alerts**; nothing leaves the LAN unless you set it. |
| `ALERT_TOKEN` | empty | no, **secret** | Optional, sent as `Authorization: Bearer ...`. |
| `ALERT_FORMAT` | `ntfy` | no | `ntfy` or `webhook`. |
| `EGRESS_CHECK_INTERVAL` | `300` | no | Seconds between exit-address checks; `0` = off. |
| `EGRESS_CONFIRM` | `2` | no | Mismatches in a row before it counts (1 to 10). |
| `HOME_PUBLIC_IP` | empty | no | Your home line's public address(es) or IPv6 prefix, comma-separated; never logged in full. Also read by the app: podcast fetches never go there (see `FETCH_BLOCKED_NETWORKS`). [How to find it](watchdog.md#finding-home_public_ip) |
| `EGRESS_EXPECTED_COUNTRY` | empty | no | If set, the VPN exit must be in this country. |

## YouTube and subscriptions ([youtube.md](youtube.md))

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `YTDLP_PROXY` | `http://gluetun:8888` | no | Where yt-dlp and the channel feeds go out: gluetun's HTTP proxy, i.e. through the VPN. **Set but empty** = ask YouTube directly from the home line (the owner's decision; unset = the default). |
| `YOUTUBE_MAX_HEIGHT` | `1080` | no | Tallest video in pixels (144 to 4320); `0` = no limit. A disk-space knob. |
| `SUBSCRIPTION_POLL_MINUTES` | `60` | no | How often each subscribed channel is checked (15 to 1440; each channel gets its own slightly different interval); `0` = never poll (the "Check now" button still works). |

## Podcasts ([podcasts.md](podcasts.md))

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `PODCAST_PROXY` | `http://gluetun:8888` | no | Where podcast feeds and episodes go out: gluetun's HTTP proxy, i.e. through the VPN. **Set but empty** = fetch directly from the home line (some podcast hosts block VPN addresses). Only `http://` proxies. |
| `FETCH_BLOCKED_NETWORKS` | empty | no | Networks podcast fetches must never go to, comma-separated, **in addition to** the private ranges, `HOME_PUBLIC_IP` and this host's own global IPv6 networks (found automatically). Put the home line's global IPv6 prefix here (DS-Lite gives the LAN native global IPv6): on the VM `ip -6 addr show scope global`. Matters when `PODCAST_PROXY` is empty (direct). Never logged in full. |
| `PODCAST_POLL_MINUTES` | `240` | no | How often each subscribed podcast's feed is checked (30 to 1440; each feed gets its own slightly different interval); `0` = never poll (the "Check now" button still works). |
| `MAGNET_STALL_DAYS` | `7` | no | Days without progress after which a magnet link is given up (job failed with "no sources for 7 days", torrent and unfinished files removed from qBittorrent; retry from the job list). Paused time and downtime do not count. `0` = never. See [app.md](app.md). |
| `APP_ADMIN_USER` | `admin` | no | The name of the first admin account of the app ([accounts.md](accounts.md)). Created at start if it does not exist. |
| `APP_ADMIN_PASSWORD_HASH` | `changeme` (= not set) | **yes**, **secret-ish** | The scrypt hash of its password, made by `make admin-password` (`scripts/app/set_admin.py`), never typed by hand. Creates the account; a changed hash resets the password of that account (recovery of a lost password). Without any account nobody can log in. |
| `AUTODEPLOY_REMOTE` | empty | no | Automatic updates ([autodeploy.md](autodeploy.md)): the GitLab repository to watch, like `http://gitlab.lan/group/project.git`, without a login in it. Empty = off. Read by the host script `scripts/ops/autodeploy.py`, not by the containers. The read-only token is in `config/autodeploy/token`, never here. |
| `AUTODEPLOY_BRANCH` | `main` | no | The branch automatic updates follow. |
| `AUTODEPLOY_HEALTH_WAIT` | `600` | no | Seconds (30 to 3600) to wait for every service to be healthy after an update before it is rolled back. |

## Image tags (pinned; [operations.md](operations.md))

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `GLUETUN_TAG` | `v3.41.3` | yes | `qmcgaw/gluetun`. |
| `QBITTORRENT_TAG` | `5.3.0_v2.1.2-ls485` | yes | `lscr.io/linuxserver/qbittorrent`; the exact build tested. |
| `CADDY_TAG` | `2.10.2` | yes | `caddy`. |

## Fixed for the stack (not for `.env`)

These are read by the app or watchdog but are either fixed in `docker-compose.yml` or left at their
defaults; they matter when running the code outside the stack (tests, development).

| Variable | Value in the stack | Meaning |
|---|---|---|
| `APP_HOST`, `APP_PORT` | `0.0.0.0`, `8000` | Where the app listens inside its container (never published). |
| `CONFIG_DIR` | `/config` | The app's folder (`config/app` on the host): the database. |
| `QBIT_URL` | `http://gluetun:8080` | qBittorrent as seen from the bridge network (it lives in gluetun's namespace). |
| `WATCHDOG_STATUS_URL` | `http://watchdog:8090` | The watchdog's status page, read by the app for the VPN bar. |
| `WATCHDOG_STATUS_PORT` | `8090` | Where the watchdog serves it. |
| `GLUETUN_CONTROL_URL` | `http://gluetun:8000` | gluetun's control server, read by the watchdog. |
| `WATCHDOG_HEARTBEAT_FILE` | `/tmp/watchdog.alive` | The file its health check reads. |
| `YOUTUBE_PROBE_TIMEOUT` | `25` | Seconds a YouTube probe may take (3 to 120). Not in the compose file: the default is used. |

## Variables of the test and ops scripts

All optional; they only change how the scripts behave, never the stack.

| Variable | Used by | Meaning |
|---|---|---|
| `KS_COMPOSE`, `KS_IP_URL`, `KS_POLL_SECONDS`, `KS_HEALTHY_TIMEOUT`, `KS_RECOVERY_TIMEOUT` | `make killswitch-test` | Compose command, the "what is my IP" URL, poll pause and timeouts ([killswitch.md](killswitch.md)). |
| `AB_COMPOSE`, `AB_PROBE_URL`, `AB_PROBE_INTERVAL`, `AB_POLL_SECONDS`, `AB_DETECT_TIMEOUT`, `AB_HEALTHY_TIMEOUT`, `AB_RECOVERY_TIMEOUT` | `make vpn-abuse-test` | The same for the abuse test. |
| `RT_COMPOSE`, `RT_IP_URL` | `make recovery-prepare` / `recovery-verify` | The compose command (default `docker compose`) and the "what is my IP" URL asked from the host for the home address (default `https://api.ipify.org`; never printed). |
| `YTP_BACKUP_DIR` | `make backup`, `create-layout.sh`, `check-host.sh` | Folder for the archives. Default `<DATA_ROOT>/.youtube-premium-backups` (on the media drive, mode 700; see [operations.md](operations.md#where-the-backups-go-decided-on-the-media-drive)). Can also be set in `.env`. |
| `YTP_PYPI_JSON` | `make update-check` | Another JSON source than PyPI (tests). |
| `YTP_NETWORK_TESTS` | `make test-system` | `1` = also run the tests that need the real internet (YouTube). |
| `YTP_YTDLP_PROXY` | those tests | Proxy for them, for example `http://127.0.0.1:8888`. |
| `YTP_VPN_ENV_FILE` | the tunnel system test | Path of a `.env` with real VPN credentials. |
| `YTP_OVPN_SOURCE` | the system-test helper | Another source for `expressvpn.ovpn`. |
| `REQUIRE_CAPABILITIES` | the test suite | `1` (set in CI) turns skipped capability tests into failures. |
