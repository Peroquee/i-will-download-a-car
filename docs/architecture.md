# Architecture overview (issue #31)

youtube-premium is a small web app on the home LAN. You paste a YouTube link or a magnet link, it
downloads in the background, and the result lands in the right Jellyfin library. Four ground rules
shape everything: **no traffic outside the VPN, LAN only behind a login, never fail silently,
recover cleanly.**

## The picture

```
   LAN browser                                   Proxmox host
        |                         +--------------------------------------------------+
        | http (port 80 / 8081)   |  Debian 12 VM  (Docker Compose, project           |
        v                         |                 "youtube-premium")                |
 +-----------------+  login +     |                                                   |
 | proxy (Caddy)   |  LAN check   |   default bridge network                          |
 +--------+--------+              |                                                   |
          | :8000          :8080  |   +-------+  /api/*   +-------------+             |
          +-------> app <--------+-> |  app  | --------> |   gluetun   | <-- VPN ----+--> ExpressVPN
          |        (FastAPI,      |   +---+---+  http     | (tunnel +   |   (OpenVPN) |    servers
          |         SQLite,       |       |   proxy :8888 |  firewall)  |             |
          |         scheduler)    |       |               |  +--------+ |             |
          |                       |       |               |  | qBitt- | | shares      |
          +--- qBittorrent UI ----+-------+-------------> |  | orrent | | gluetun's   |
                                  |       |  :8080        |  +--------+ | network     |
                                  |   +---+------+        +------^------+             |
                                  |   | watchdog | -- status + key --+                |
                                  |   +----------+  pauses torrents if the VPN drops  |
                                  +----------+---------------------------------------+
                                             | /data (one filesystem: hardlinks)
              Jellyfin (separate LXC) <------+  downloads/  Filme/  Serien/  Hörbücher/  Musik/  Youtube/
              refresh via its API
```

Everything but Jellyfin is one Compose stack in one VM. Jellyfin runs in its own LXC and only
sees the media folders; the app calls its API to refresh a library.

## The services

| Service | What it does | Network | Notes |
|---|---|---|---|
| `gluetun` | The VPN tunnel and a firewall that blocks everything not going through it. Also an HTTP proxy for yt-dlp (`:8888`, inside the stack only) and a control server (`:8000`, read-only key for the watchdog). | default bridge; publishes the qBittorrent UI port on `127.0.0.1` only | healthy only while the tunnel works |
| `qbittorrent` | Torrent client. | **gluetun's network namespace** (`network_mode: service:gluetun`) | no network of its own: if gluetun is down it has none. Starts only when gluetun is healthy. |
| `app` | FastAPI app (`app/ytp`): API, web page, SQLite job queue, schedulers, workers. | default bridge | runs as `PUID:PGID`; no published port |
| `proxy` | Caddy: the only way in. Refuses clients outside `LAN_SUBNET`, then asks for the login. Port 80 = the app, 8081 = the qBittorrent UI. | default bridge; published on `PROXY_BIND` only | starts after the app is healthy |
| `watchdog` | Second layer: pauses all torrents when the VPN drops, checks the exit address, sends alerts, serves the status for the page's VPN bar. | default bridge | same image as the app; read-only, no capabilities |

Start order, enforced by `depends_on` and health checks: **gluetun -> (healthy) -> qbittorrent**,
and **app -> (healthy) -> proxy**. The watchdog starts independently and reports unhealthy until it
can read the VPN.

## What the app does (inside `app/ytp`)

| Part | Modules | Purpose |
|---|---|---|
| Web page and API | `web.py`, `static/`, `api.py`, `jobs_api.py`, `subscriptions_api.py` | the form, job list, subscriptions, VPN bar; same-origin check on every state-changing request |
| Job queue | `db.py`, `jobs.py`, `migrations/` | SQLite in WAL mode; the table `jobs` is the queue and the record of what was ever downloaded |
| Torrents | `magnet.py`, `clients/qbittorrent.py`, `reconciler.py`, `organizer.py` | add a magnet in the right category; when qBittorrent finishes, **hardlink** the files into the library folder and refresh Jellyfin |
| Podcasts | `podcasts.py`, `podcast_subscriptions.py`, `podcast_jobs.py`, `podcasts_api.py`, `safefetch.py` | RSS feeds and episodes from arbitrary hosts through the VPN proxy, public addresses only ([podcasts.md](podcasts.md)); files into `Podcasts/<podcast>/` |
| YouTube | `youtube.py`, `youtube_download.py`, `youtube_jobs.py` | probe (title, channel), download with yt-dlp through gluetun's proxy, file into `Youtube/<channel>/` |
| Subscriptions | `channels.py`, `subscriptions.py`, `feeds.py`, `subscription_poller.py` | resolve a channel once, poll its RSS feed slowly, queue only videos published after subscribing |
| Alerts and dashboard | `alertlog.py`, `monitor.py`, `alerts_api.py`, `dashboard_api.py`, `static/dashboard.js` | an alert log written by a scheduled monitor (VPN changes seen through the watchdog's status, failed jobs, feed errors, disk space), the strip under the VPN bar and `/dashboard` ([dashboard.md](dashboard.md)) |
| Jellyfin | `clients/jellyfin.py` | debounced library refresh |
| VPN view | `clients/watchdog.py` | reads the watchdog's status for the page |
| Scheduler | `scheduler.py` | the one place that runs periodic jobs (reconciler, poller, Jellyfin refresh, heartbeat) |
| Watchdog | `watchdog.py`, `egress.py`, `alerts.py`, `clients/gluetun.py` | the separate container's code |

## How things flow

**A magnet link.** Browser -> proxy (login) -> `POST /api/magnet` -> a `jobs` row (unique per
infohash, so the same torrent is never added twice) -> qBittorrent, in the category of its type
(`movies`, `series`, `audiobooks`, `music`) -> downloads into `/data/downloads/<category>` ->
the reconciler notices it is finished -> the organizer **hardlinks** it into
`/data/<library folder>/...` (never moves: seeding continues) -> Jellyfin refresh.

**A YouTube link.** `POST /api/youtube/probe` asks yt-dlp (as a library, never a shell; YouTube
addresses only) for title and channel, through gluetun's proxy. You confirm the names (they become
folder names and are checked) -> `POST /api/youtube/download` queues a job -> one worker thread
downloads and files it -> Jellyfin refresh.

**A subscribed channel.** The channel id is resolved once. Every minute the poller looks at the
channels that are due (about hourly each, spread out), reads the RSS feed through the same proxy,
and queues videos published after the subscription as ordinary YouTube jobs.

**Failures are visible.** A job is `queued`, `downloading`, `organizing`, `done` or `failed`; a
failed job carries its reason, shows in red in the job list and can be retried with one click.

## Where the data lives

| What | Where | Backed up |
|---|---|---|
| Downloads and libraries | `DATA_ROOT` (`/data`), one filesystem so hardlinks work | no (it is on the share) |
| Database | `config/app/app.db` | yes ([operations.md](operations.md)) |
| gluetun, qBittorrent, proxy state, VPN credentials file | `config/` | yes |
| Secrets | `.env` (never in Git) | yes |
| Logs | Docker's json-file driver, 10 MB x 3 per container | no |

## The security model in one page

- **VPN, in layers.** qBittorrent has no network of its own, only gluetun's, and gluetun's firewall
  drops anything that does not go through the tunnel. qBittorrent is also bound to `tun0`. The
  watchdog pauses all torrents within seconds of the VPN dropping, and a slow egress check
  confirms the public address is not the home line's. [killswitch.md](killswitch.md) proves it.
- **LAN only, behind a login.** Only the proxy publishes ports, on one LAN address. It refuses
  other networks and asks for a login (bcrypt hash in `.env`). The app and qBittorrent's UI are not
  reachable any other way. [proxy.md](proxy.md).
- **A web page that cannot be turned against you.** Every POST/PUT/PATCH/DELETE must come from
  this site (`Sec-Fetch-Site` / `Origin` check), the page loads nothing from other sites, the
  content security policy allows only its own scripts and styles.
- **Untrusted input.** Names become folder names only after a strict check; YouTube addresses are
  parsed and rebuilt before yt-dlp sees them; the RSS feed is refused if it has a DTD and read as
  UTF-8 only; the magnet link is never logged (it may carry a tracker passkey).
- **No Docker socket anywhere.** The recovery of qBittorrent after gluetun restarts is a small
  watcher inside its own container, not something with control over Docker.
- **Secrets** live in `.env` and `config/` (mode 600), never in Git; a pipeline job scans for them.

## Repository layout and tests

See the README for the folders. Tests are in five levels (`tests/unit` to `tests/regression`,
[testing.md](testing.md)); the unit tests need nothing but Python, the system tests need the VM.
The CI pipeline (GitLab) runs lint (ruff, yamllint, shellcheck, gitleaks), the unit and the
integration tests on every push.
