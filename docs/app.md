# The app (issue #8)

A small FastAPI service. This first step is the scaffold only: configuration, structured
logging and a health endpoint. The database (#9), the qBittorrent and Jellyfin clients
(#10, #11) and the scheduler (#12) come in later issues.

## Layout

```
app/
  Dockerfile           python:3.12-slim-bookworm, pinned requirements, health check
  requirements.txt     fastapi + uvicorn and everything they pull in, all pinned
  ytp/                 the package: config.py, logging_setup.py, main.py, __main__.py
```

Dependencies are deliberately few: **FastAPI** (the web framework) and **uvicorn** (the server,
without the `[standard]` extras, so nothing is compiled). The settings use a plain dataclass,
not pydantic-settings. The test client (`httpx2`) is only in `requirements-app.txt`.

## Configuration

Read once at startup from environment variables (names as in `.env.example`; compose passes
the app the ones it needs):

| Variable | Meaning | Default |
|---|---|---|
| `MEDIA_DIR_MOVIES`, `_SERIES`, `_AUDIOBOOKS`, `_MUSIC`, `_YOUTUBE` | library folder names relative to `/data` | required |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY` | the external Jellyfin (docs/jellyfin.md) | empty: refreshes disabled, warned in the log |
| `QBIT_URL`, `QBIT_USER`, `QBIT_PASSWORD` | qBittorrent's Web UI (over the bridge: `http://gluetun:8080`) and its login | password empty: downloads disabled, warned in the log |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `DATA_ROOT`, `CONFIG_DIR` | where the volumes are mounted in the container | `/data`, `/config` |
| `APP_HOST`, `APP_PORT` | listen address inside the container | `0.0.0.0`, `8000` |

A bad value does not start the app: it logs one JSON error listing **every** problem and exits
with code 2, so a wrong `.env` is fixed in one go. The API key is never printed (not in the
settings' repr, the startup summary or any log line: fields named key/token/password/secret/
authorization/cookie are masked).

## Logging

One JSON object per line on stdout (`docker compose logs app`): `ts` (UTC), `level`, `logger`,
`msg`, plus any `extra=` fields. Docker's health-check requests are not logged.

## /health

`GET /health` answers `200 {"status": "ok", ...}` when the app is fine and `503
{"status": "degraded", ...}` when it is not (today: `/data` is not a directory, for example a
detached media volume). The image's `HEALTHCHECK` calls it, so `docker compose ps` shows
`healthy` or `unhealthy`. It deliberately does not call qBittorrent or Jellyfin: both depend on
the VPN, and a check that reached for them would go red whenever the tunnel reconnects.

There is no `/docs`, `/redoc` or `/openapi.json`.

## Reachability

The app is on the default bridge network and **publishes no port**, so it cannot be reached
from outside the host. The reverse proxy with login (#29, [proxy.md](proxy.md)) reaches it as
`http://app:8000` over the bridge, and is the only way in from the LAN: `http://<vm>/`. To
look at it directly, from the VM:

```bash
docker compose exec app python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/health').read().decode())"
```

It runs as `PUID:PGID` (`user:` in compose); the image has no user of its own and writes
nothing at runtime except its database in `/config` (next section).

## qBittorrent client (issue #10)

`ytp.clients.qbittorrent.QbitClient` (standard library only, synchronous; async code calls it
with `asyncio.to_thread`). One instance per app, `app.state.qbit` (`None` when
`QBIT_PASSWORD` is empty). It never contacts qBittorrent at startup.

| Call | What it does |
|---|---|
| `add_magnet(magnet, category)` | validates the link (`ytp.magnet`: one single-line `magnet:?` link with one v1 infohash, no whitespace, so nothing can ride along in the `urls` field) and the category (`movies`, `series`, `audiobooks`, `music`), sends both as form fields with `autoTMM=true`, returns the lower-case infohash |
| `list_torrents(state=None, category=None, hashes=None)` | `Torrent` objects; `state` is a `torrents/info` filter (`downloading`, `completed`, ...); `paused`/`stopped` and `resumed`/`running` fall back to each other on 400 (qBittorrent 4 vs 5) |
| `pause_all()` / `resume_all()` | `torrents/stop` / `start`, falling back to `pause` / `resume` on 404 |
| `version()` | proves login and network work |

Failure handling, all as typed exceptions (never a `ConnectionError`):

- `QbitUnavailable`: refused, reset, DNS, timeout (10 s on every call), a 5xx, a garbled answer.
  Expected whenever the VPN is down; the caller degrades and tries again later.
- `QbitAuthError`: no password set, wrong credentials, banned address, or a session that is
  still refused after one re-login. **A refused login is never retried automatically**
  (five failures ban the address for an hour): wrong credentials stay refused until the app is
  restarted with the right ones; after a ban (403) the next attempt waits an hour.
- `QbitRequestError`: qBittorrent answered but refused (a magnet it won't take, an unknown call).
- An expired session (403) is handled inside: log in again once, repeat the call once.
- Bad input (`InvalidMagnet`, an unknown category or state) is a `ValueError` raised before
  anything is sent.

The client sends `Origin`/`Referer` equal to its base URL and a matching `Host`, which is what
qBittorrent's CSRF and host checks need. The password and the session cookie are never logged.

## Database (issue #9)

The app keeps its jobs and YouTube subscriptions in one SQLite file, `/config/app.db`
(`./config/app/app.db` on the host), opened at startup with:

- **WAL mode**, so the polling scheduler and a request do not block each other. (WAL needs a
  local disk; `/config` is on the VM's own disk, not on the media share.) The files
  `app.db-wal` and `app.db-shm` next to it belong to the database: copy all three, or none,
  and never delete them while the app runs.
- `synchronous=NORMAL`, `foreign_keys=ON`, and a 5 s wait for the write lock instead of an
  instant "database is locked".
- **Migrations**: `app/ytp/migrations/NNNN_name.sql`, applied in order at every start. Each
  file and its bookkeeping row (`schema_migrations`: version, name, SHA-256) are one
  transaction, so a failing migration leaves the database on the previous version. The app
  **refuses to start the database** (and says so) if an applied file was edited afterwards, or
  if the database is from a newer version than the image. To change the schema, add a file;
  never edit one that has shipped.

Tables (`0001_initial.sql`): `jobs` (type `magnet`|`youtube`, `external_id` = infohash or video
id, unique per type; `metadata` is a JSON object; `status` queued, downloading, organizing, done
or failed; `error`; `created_at`, `updated_at` as UTC text, the latter kept current by a
trigger) and `subscriptions` (`channel_id` unique, `channel_name`, `last_checked`,
`last_error`, `enabled`). Python: `ytp.db.Database` (one connection with a lock;
`transaction()` for writes, `query()` for reads), no ORM.

### The folder must belong to the app user

The app runs as `PUID:PGID` and cannot create or `chown` `/config`. If `./config/app` does not
exist, Docker would create it as `root:root` and the app could not write to it. So:

- `docker-compose.yml` mounts it with `create_host_path: false`: a missing folder is an error at
  `docker compose up`, not a root-owned folder.
- `sudo scripts/host/create-layout.sh` (`make layout`) creates `config/app` owned by
  `PUID:PGID`, mode 2775, and **hands over** an existing one (for example the `root:root` one
  from the first start of the app) to `PUID:PGID`: that folder and `app.db*` only, not
  recursively. It runs as root, so it **refuses** a symlinked `config/app` or `app.db*` and an
  `app.db*` with several hard links, and changes nothing in that case. `check-host.sh --only layout` fails with that hint when the owner is wrong.
- If it is still wrong, the app does not crash-loop: it stays up **degraded**. The log has
  `database unavailable` with the reason and the fix, `/health` answers `503` with
  `"database": "unavailable: ..."`, and Docker shows it `unhealthy`. Fix the folder, then
  `docker compose restart app`.

Fix on the VM that currently has the `root:root` folder:

```bash
sudo scripts/host/create-layout.sh          # shows "fixing config/app" and hands it to PUID:PGID
docker compose up -d app                    # picks up the new image / restarts; app turns healthy
```

## Build and run

```bash
docker compose build app          # builds app/Dockerfile
docker compose up -d app
docker compose ps                 # app: healthy after about 15 seconds
docker compose logs app           # JSON lines
```

For development without Docker: `make venv`, then `cd app && python -m ytp` with the
variables above set.

## Job metadata (issue #14)

`ytp.metadata` defines what the user must say about a download, per type. `type` picks the
schema and uses the qBittorrent category names (`movies`, `series`, `audiobooks`, `music`):

| `type` | required | optional |
|---|---|---|
| `movies` | `title`, `year` (1870 to next year) | |
| `series` | `title`, `season` (1 to 9999) | `episode` (1 to 9999); none = a season pack |
| `audiobooks` | `author`, `title` | |
| `music` | `artist`, `album` | |

`parse_metadata(dict)` returns the model or raises pydantic's `ValidationError` with one
error per field (`loc`, a machine-readable `type` such as `missing`, `name_empty`,
`year_out_of_range`, `extra_forbidden`, and a readable `msg`); used as a FastAPI body
(`body: Metadata`) it becomes the usual 422 response. Unknown extra fields are refused.
`metadata_to_json` gives the JSON object stored in `jobs.metadata` (no nulls, so a season pack
has no `episode` key); `metadata_from_json` reads it back.

All text values go through `safe_path_component` because they become folder names (#16): it
strips, normalises to NFC (one "ö", not "o" + diaeresis), and refuses empty or longer than
200 characters, `/` and `\`, control/invisible characters (newline, tab, NUL, bidi overrides),
and names of only dots. A colon, quotes, a leading dot and umlauts are fine; making a name safe
for a particular file system is #16's job. #16 reuses this function for anything else it
builds a path from.
