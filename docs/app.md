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
| `list_torrents(state=None, category=None, hashes=None)` | `Torrent` objects; `state` is a `torrents/info` filter (`downloading`, `completed`, ...), which is broader than its name: `downloading` also lists paused (`stoppedDL`), queued and stalled torrents, so check `Torrent.state` / `.paused` where it matters; `paused`/`stopped` and `resumed`/`running` fall back to each other on 400 (qBittorrent 4 vs 5) |
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

## Magnet endpoint (issue #13)

`POST /api/magnet` (through the proxy, so behind its login):

```bash
curl -u admin http://<vm>/api/magnet -H 'Content-Type: application/json' -d '{
  "magnet": "magnet:?xt=urn:btih:<40 hex or 32 base32>&dn=...",
  "metadata": {"type": "movies", "title": "Big Buck Bunny", "year": 2008}
}'
```

`metadata` is the per-type object of [Job metadata](#job-metadata-issue-14); its `type`
(movies, series, audiobooks, music) is the media type and picks the qBittorrent category. The
link is stripped and validated (`ytp.magnet`); errors are field-level 422s
(`loc: ["body","magnet"]`, `["body","metadata","movies","year"]`, ...).

| Answer | Meaning |
|---|---|
| `201 {"id", "infohash", "type", "status": "queued", "duplicate": false}` | new job, handed to qBittorrent |
| `200 {... "duplicate": true}` | the infohash is already in the system (any status except `failed`): the existing job is returned, nothing is added again, its metadata is not changed |
| `422` | invalid link or metadata |
| `503 {"detail", "job_id"}` | qBittorrent unreachable or its login refused (or the database / qBittorrent is not configured; then no job is created) |
| `502 {"detail", "job_id"}` | qBittorrent answered but would not take the torrent |

The job row is written first, keyed by the infohash (the unique index decides duplicates, so two
simultaneous submissions create one job), then qBittorrent is asked. If that fails the job is
marked `failed` with the reason, and **sending the same magnet again retries it** (same job id,
status `queued` again, new metadata, answered `201`). A hex, an upper-case or a base32 form of
the same infohash is the same torrent. The log has the job id and infohash, never the link
(trackers can carry a private passkey).

## Scheduler (issue #12)

APScheduler 3 (`AsyncIOScheduler`) runs inside the app's event loop; `ytp.scheduler.AppScheduler`
starts with the app (once the database is open) and stops with it. Later jobs (the reconciler
#15, the YouTube poller #20, the VPN watchdog #21) are registered through
`AppScheduler.add_interval_job(func, job_id=..., seconds=...)`, which gives every job:

- **no overlap**: `max_instances=1` and `coalesce=True`. A run that is still going when the next
  one is due makes that one be skipped (a warning `job skipped: the previous run is still going`
  is logged) and a backlog of missed runs collapses into one;
- **failure containment**: an exception is logged (`job failed`, with the traceback) and the job
  stays scheduled; other jobs are unaffected;
- **no blocking**: plain functions run in worker threads, coroutine functions on the loop.

Jobs are registered **by the code at every start** (`replace_existing`), not stored in a job
store, so after a restart or an update the schedule is exactly what the code says. State that a
job must keep between runs lives in the database (`app_state`, `jobs`, `subscriptions`). I chose
this over APScheduler's SQLAlchemy job store, which would add SQLAlchemy to the image to store
schedules that the code already defines. "Survives a restart" is shown by the placeholder job:
the **heartbeat** (every 30 s, first run at once) writes `scheduler.last_heartbeat` to
`app_state`; at start the app logs `scheduler started` with the `previous_heartbeat` the last run
left behind.

`/health` has `checks.scheduler`: `ok`, `stalled: no heartbeat for N s` (after three missed
heartbeats, which also catches a stuck event loop; the app turns unhealthy), or
`disabled: no database`. APScheduler's own per-run INFO lines are silenced (a heartbeat would be
5,760 lines a day).

## Jellyfin client (issue #11)

`ytp.clients.jellyfin` (standard library, like the qBittorrent client) talks to the external
Jellyfin at `JELLYFIN_URL` with `JELLYFIN_API_KEY`. The key is sent in the `Authorization:
MediaBrowser Token="..."` header, never in a URL, a log line or an error. Calls: `ping()` (public
info, no key), `list_libraries()`, `refresh_library()` (scan everything) and `refresh_item(id)`
(scan one library). Errors are typed: `JellyfinUnavailable` (unreachable, timeout, 5xx),
`JellyfinAuthError` (401/403: key missing or revoked), `JellyfinRequestError`.

**Using it:** other code calls `app.state.jellyfin.request_refresh("Filme")` (the library folder
name, as in `MEDIA_DIR_*`; no argument = "somewhere"). That call only notes the request. It never
raises and never waits for Jellyfin, so a refresh problem cannot fail a download job.

- **Debounce:** the scheduler job `jellyfin-refresh` calls `flush_due()` every 5 s. A batch of
  requests becomes ONE refresh per affected library, sent after 30 s of quiet (but at the latest
  300 s after the first request, so a steady trickle is still refreshed).
- **Which library:** found by the last component of one of its locations, compared as NFC text
  and case-insensitively (`Filme` matches `/media/jellyfin/Filme`; `Hörbücher` is the same typed
  with a combining diaeresis). No path is translated between `/data` and `/media/jellyfin`. A
  folder that no library has gets a full refresh, so nothing is silently missed.
- **Fail soft:** Jellyfin down or answering 5xx: the batch is kept and retried every 60 s, up to
  5 attempts, then dropped with an error in the log. A revoked key: logged once, batch dropped,
  not retried. No URL or key configured (a startup warning says so): everything is a no-op.
- Pending refreshes live in memory: a restart loses them, and Jellyfin's own library scan finds
  the files later.

## Completion reconciler (issue #15)

The scheduler job `reconcile` (every 30 s, started at once, only when qBittorrent is configured)
keeps the unfinished magnet jobs in step with qBittorrent (`ytp.reconciler.Reconciler`):

```
queued -> downloading -> organizing -> done          any of them -> failed (with a reason)
```

It polls the Web API instead of using qBittorrent's "run on completion" hook (silent-failure
reports; it would run inside gluetun's namespace and need a hole in the VPN firewall).

| qBittorrent says | the job |
|---|---|
| the torrent is there and downloading | `queued` becomes `downloading` |
| finished (`progress` 1.0) and settled | claimed as `organizing`, the organizer runs, then `done` |
| `moving` (leaving the incomplete folder) or `checking*` | wait; the path is not final yet |
| `error` / `missingFiles` | `failed`: "qBittorrent reports the torrent as 'error'" |
| the torrent is gone | `failed`: "no longer in qBittorrent ... send the magnet link again" |
| gone, and the job is `queued` for over 2 min | `failed`: "never reached qBittorrent (the app stopped before adding it)" (the endpoint writes the job before it asks qBittorrent) |

- **Idempotent:** every run starts from what is true now, so a torrent that finished while the
  app was down is organized at the first run; a `done` job is never touched; the claim is a
  conditional UPDATE. The organizer (#16) must tolerate being run again for the same job.
- **Never stuck in `organizing`:** the organizer runs in a `try`; an `OrganizeError` (or any
  other exception) makes the job `failed` with `organizing failed: <reason>`. If the app is killed
  in the middle, the next run finds the leftover `organizing` job (nothing else organizes at the
  start of a run) and organizes it again.
- **qBittorrent away** (the VPN reconnects): nothing changes, no job is failed; the outage and
  the recovery are logged once each. A refused login is logged once and not retried (ban
  protection of the client).
- **Without an organizer** (until #16 sets `app.state.organizer`): finished torrents stay
  `downloading`, with one warning in the log; nothing is lost.
- `app_state` key `reconciler.last_ok` has the time of the last successful run.
- A torrent that never finds metadata (a magnet nobody seeds) stays `downloading`; a timeout for
  that is not part of this issue.

## File organizer (issue #16)

`ytp.organizer.FileOrganizer` is the `organize(job, torrent)` the reconciler (#15) calls for a
finished torrent. It reads the job's metadata (#14), builds the target paths, links the wanted
files into the library folder of the type (`MEDIA_DIR_*` under `/data`) and asks Jellyfin for a
debounced refresh of that library (`request_refresh(<folder>)`, #11).

| type | target |
|---|---|
| movies | `Filme/Title (2008)/Title (2008).mkv`, subtitles next to it keep their language suffix (`Title (2008).de.srt`) |
| series | `Serien/Title/Season 03/Title - S03E07.mkv` |
| audiobooks | `Hörbücher/Author/Title/<audio files as they are, disc folders kept>` + `cover.jpg`/`folder.jpg` |
| music | `Musik/Artist/Album/<audio files as they are, disc folders kept>` + cover |

- **Hardlink, never move.** One file on disk, two names; the torrent keeps seeding from its
  original path, which is never touched. If the two folders are on different filesystems
  (`EXDEV`) the file is copied (via a temporary name) with a warning in the log; any other link
  error is a failure with the system's message, never a silent copy.
- **Series:** a single episode uses the metadata (season and episode) whatever the file is called;
  a **season pack** maps every video by the number in its name: `S03E07`, `s3e7`, `3x07`,
  `S03E07E08` / `S03E07-E09` (written `S03E07-E08`), or `E05`/`Episode 06` with the season from a
  `Season N`/`Staffel N` folder, else from the metadata. A season in the file name wins over the
  metadata (it describes the content). Two files for one episode: the larger is kept. Files
  without an episode number are skipped (with a note in the log); a pack with none at all fails.
- **Movies:** the largest video file; further videos are treated as extras and not linked.
- **Junk is not linked:** `.nfo`, `.txt`, `.url`, checksums, executables, posters, sample files
  (a `sample` word in the name or a `Sample/` folder), screenshot folders; video types take only
  videos and subtitles, audio types only audio and covers.
- **Repeatable and careful:** a target that already is the same file is fine; a different file
  there is never overwritten (skipped and logged; if nothing at all could be linked the job fails
  with "already exist in the library with other content"). Missing downloads, a missing library
  folder, no video/audio, or invalid stored metadata fail the job with a readable reason.
- **Untrusted file names:** symlinks in the torrent are never followed; the source must be inside
  `downloads/` and every target inside its library folder; every name passes
  `safe_path_component` (names with control characters or over 255 bytes are skipped).
- New folders honour `UMASK` from `.env` (the app applies it at start): 002 keeps them
  group-writable; Jellyfin reads through the "other" bits.

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

## The submission form (issue #25)

`GET /` (through the proxy: `http://<vm>/`) is the form: one text field for a link, a
YouTube/Magnet toggle, and the fields of the chosen branch. Plain HTML, CSS and two small scripts
from `app/ytp/static/`; **nothing is loaded from another site** (no CDN, no web font; a test
fails if a page file mentions one) and the content security policy allows only this site's own
scripts and styles.

- **Paste detection:** a `magnet:` link switches to the Magnet branch, a YouTube address to the
  YouTube branch; the toggle changes it by hand.
- **YouTube branch:** on paste the page asks `POST /api/youtube/probe` and fills **channel** and
  **title** (editable; if the lookup fails, they can be typed). Submit sends the confirmed names to
  `POST /api/youtube/download`. The thumbnail is deliberately **not shown**: it would be loaded from
  YouTube by the browser on the home connection. Ticking **Subscribe to the channel** also calls
  `POST /api/subscriptions` with the video's address and the confirmed channel name once the
  download is queued (see youtube.md, "Subscriptions"); the result line says whether it worked,
  and a failed subscription never undoes the download.
- **Target (issues #35, #36):** a YouTube download can be filed as a *Podcast episode* or as
  *Music* (artist, track, album) instead of a video, optionally *Audio only*; the two name fields
  then mean podcast and episode, or artist and track. The
  *Subscribe* checkbox and the subscription form take the same choice. See
  [youtube.md](youtube.md#as-a-podcast-episode-optionally-audio-only-issue-35).
- **Magnet branch:** a media type (movie, series, audiobook, music) swaps the fields: movie title +
  year; series title + season + episode, or "whole season" (no episode); audiobook author + title;
  music artist + album. Submit sends `POST /api/magnet`.
- **Errors per field:** the server's 422 answers are placed under the right input (the same names
  the API uses), with a short check in the browser first; failures of the services (YouTube not
  reachable, VPN, qBittorrent) appear as a message under the button.
- **German and English** (text in `index.html`, chosen from the browser language, switch at the top
  right, remembered in the browser). Umlauts work everywhere (UTF-8).
- **Mobile:** one column under 520 px, 44 px touch targets, 16 px inputs (phones do not zoom in).
- The **VPN bar** (issue #28) sticks to the top of the page, so it is always in view; see below.

**Same-origin protection (the note on !20):** the JSON endpoints are state-changing, and the
browser holds the proxy login, so a page on another website could otherwise make it add downloads.
Every POST/PUT/PATCH/DELETE is refused with 403 unless the browser says the request comes from this
site: `Sec-Fetch-Site` must be `same-origin` or `none`, and an `Origin` header must name the same
host and port as `Host` (Caddy passes `Host` through). `Origin: null` is refused. Requests with
neither header are not from a page in a browser (curl, scripts) and are allowed, since they have to
pass the proxy's login like everything else. The JSON endpoints also need
`Content-Type: application/json`, which a cross-site HTML form cannot send. No CORS headers are sent.
Security headers on every answer: `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`.

Not part of this form: the how-to guide (#33).

## Podcasts in the form (issue #34)

A third type, **Podcast**, is picked automatically for any web address that is not YouTube. On
paste the page asks `POST /api/podcasts/probe` and fills the folder name and a list of episodes
(newest first); *Add* sends `POST /api/podcasts/episode` for the chosen episode and/or
`POST /api/podcasts` for the subscription, one request each, and reports both results. The
subscription list shows channels and podcasts together (`GET /api/subscriptions` and
`GET /api/podcasts`). Episode titles come from a stranger's feed: they are set with `textContent`
only (a test fails on `innerHTML`). The API, the file layout and the fetcher's safety rules are in
[podcasts.md](podcasts.md).

## VPN indicator (issue #28)

A bar across the top of the page, sticky while scrolling, shows the state from `GET /api/vpn`
(the watchdog's status, see watchdog.md). It asks every 5 s while the tab is visible, and once
when the tab comes back.

| View | Bar | When |
| --- | --- | --- |
| ok | calm green line, "VPN running", how long ago it changed, exit country | watchdog `ok` (and the egress check not alarming) |
| down | **solid red band**, "VPN down - downloads paused", "torrents paused" if the watchdog paused them | watchdog `down` |
| mismatch | **solid red band**, "WARNING: traffic is not going through the VPN" | the egress check (#23) reports a mismatch |
| blind | **solid red band** | the watchdog cannot read the VPN state (treated like down) |
| starting, unknown | gold line | the watchdog has not measured yet / does not answer |
| stale | gold line "Status out of date" | the page's own request to the app failed |

Anything the page cannot read counts as unknown, never as ok. In the red states the tab title gets
a warning sign and the icon turns red (`favicon-alert.svg`), so it is visible from another tab too,
and the bar gets `role="alert"` for screen readers. No sound or animation. The exit country is the
only detail from the egress check shown; addresses are never sent to the page.

## Subscription view (issue #27)

Below the job list: **Subscribed channels**.

- **Add:** one field for an `@handle`, a channel link or the link of one of its videos, and "Also
  take older videos" (0 to 15, default 0 = only videos published after subscribing). It calls
  `POST /api/subscriptions`; a bad address is shown under the field, a failed lookup (YouTube not
  reachable, channel not found) as a message.
- **List:** each channel with when it was last checked (or "never checked") and, in red, the last
  error the poller recorded (for example that the feed could not be fetched because the VPN is
  down). Refreshes every 30 s while the tab is visible and after every action.
- **Check now:** `POST /api/subscriptions/{id}/poll` looks at that channel's feed at once,
  whatever its interval, and queues new videos like the scheduled poll; the row says how many were
  queued (they show up in the job list). It works even with `SUBSCRIPTION_POLL_MINUTES=0`. One
  manual check runs at a time (429 otherwise); a failure answers 502/503 with the reason, which is
  also stored as the channel's last error.
- **Remove:** two clicks ("Remove", then "Really remove?" for 5 seconds), no dialog. Videos that
  were already downloaded stay.

Ticking "Subscribe to the channel" in the YouTube form (#19) refreshes this list too.

## Job list and retry (issue #26)

Below the form the page lists the jobs, newest first (50 at a time), and refreshes itself every
5 seconds. It stops while the browser tab is hidden and catches up when the tab comes back, so an
open tab on a phone does not keep asking.

Each row shows the name (movie "Title (Year)", series "Title S03E07" or "S03", audiobook
"Author - Title", music "Artist - Album", YouTube "Channel - Title"), the status (queued,
downloading, being filed, done, failed), the age, and for a running download a **progress bar**.
Torrent progress comes from qBittorrent (one call, kept for 3 seconds, so several viewers do not
multiply it; if qBittorrent cannot be asked the bar is simply missing, never an error). YouTube
progress is kept in memory by the download worker (it is gone after a restart, and a download that
restarts begins again anyway).

**Failures are never silent.** A failed job is outlined in red and shows the reason the app
recorded (qBittorrent unreachable or refusing, yt-dlp's classified message, a missing file, ...;
the reason texts are English, as the logs are). A red banner above the list says how many jobs have
failed, with a button "Failed only" for when they have scrolled out of the 50. A failed job has a
**Try again** button.

- `GET /api/jobs?limit=50&status=failed` returns `{"jobs": [...], "counts": {...}}`; `counts` always
  covers all jobs, whatever the filter. `limit` is clamped to 1-200, an unknown `status` is a 422.
  The answer never contains the magnet link.
- `POST /api/jobs/{id}/retry` works only for a `failed` job (409 otherwise, 404 if unknown). A
  torrent goes to qBittorrent again; a video goes back into the download queue. If qBittorrent
  refuses or is unreachable the job is `failed` again with the new reason (502/503, as for
  `POST /api/magnet`). The route is covered by the same-origin check like every other POST.

**The magnet link is kept for retries.** A retry needs the trackers, which the infohash alone does
not have, so migration `0003_job_source` adds `jobs.source` and the magnet endpoint stores the
original link there. It can contain a tracker passkey, so it is read only by the retry, never
returned by the API, never logged. Jobs made before the migration have no stored link; their retry
falls back to a bare `magnet:?xt=urn:btih:<hash>` (DHT and peer exchange find peers, but a private
tracker would need the link sent again). The database is `config/app.db`; it is covered by the
backups (#30) like the other secrets in `config/`.

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
