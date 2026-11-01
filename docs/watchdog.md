# VPN watchdog (issue #21)

A second safety layer. **Gluetun's firewall is what really prevents leaks**: it blocks every
packet that does not go through the tunnel. The watchdog is a separate, tiny container that also
watches the VPN and, when it drops, tells qBittorrent to **pause all torrents** straight away,
whatever the app is doing. It keeps working when the app is stuck, because it shares nothing
with it but the image.

```
watchdog ──every second──► gluetun:8000  GET /v1/vpn/status   (read-only key)
   │   3 failures in a row
   └────────────────────► gluetun:8080  qBittorrent API: pause all
```

## Set up (once, on the VM)

1. `QBIT_USER` / `QBIT_PASSWORD` in `.env` must be set (the same login the app uses).
2. `make watchdog-key`: creates a random key, writes it to `.env` as `GLUETUN_CONTROL_APIKEY`
   and writes `config/gluetun/auth/config.toml`, a gluetun role that may do **exactly one thing**,
   `GET /v1/vpn/status` (it cannot stop the VPN or change anything). Mode 600, never in Git, never
   printed. Re-running keeps the key; `--rotate` makes a new one.
3. `docker compose up -d --force-recreate gluetun watchdog` (gluetun reads its auth file only at
   start; `up -d` builds the watchdog from the app image).
4. `docker compose ps`: the watchdog must be `healthy`. If it says `unhealthy`, look at
   `docker compose logs watchdog`.

Gluetun's control server stays private: all its routes need authentication, the port is not
published, and only containers on the stack's bridge network can reach it.

## Behaviour

| Situation | What it does |
|---|---|
| status `running` | nothing (it notes every 30 s which torrents are running) |
| 1 or 2 failed polls | nothing: a blip does not thrash (`WATCHDOG_THRESHOLD`, default 3) |
| threshold reached (status not `running`, or gluetun unreachable) | logs an error and pauses all torrents at once |
| qBittorrent cannot be reached to pause | retries every 5 s, logged once per outage. (qBittorrent shares gluetun's network: while gluetun is down it is unreachable, and then it cannot send anything anyway) |
| VPN is back | logs it. With `WATCHDOG_RESUME_AFTER=0` (default) the torrents **stay paused** until you resume them in qBittorrent |
| `WATCHDOG_RESUME_AFTER=N` seconds, VPN stable that long | resumes **only the torrents that were running** before the pause; ones you had paused yourself stay paused. Any failed poll restarts the wait |
| the control server answers 401/403/404 (key or auth file wrong) | **does not pause**: it cannot see, which is not evidence of an outage. It logs a loud error once, reports `unhealthy` to Docker, and keeps trying; fix with `make watchdog-key` |
| no `QBIT_PASSWORD` or key | exits with code 2 and a JSON error listing what is missing (it would protect nothing) |

Settings in `.env`: `WATCHDOG_INTERVAL` (seconds between polls, default 1),
`WATCHDOG_THRESHOLD` (default 3), `WATCHDOG_RESUME_AFTER` (default 0 = never).

Only changes are logged, not every poll: `docker compose logs watchdog` shows `VPN unhealthy:
pausing all torrents`, `all torrents paused`, `VPN is running again`, `torrents resumed`.

## Alerts (issue #22)

Off by default. Set `ALERT_URL` in `.env` to get a message when the VPN goes down **and** when
it is back (and when the watchdog itself cannot read the VPN status):

```
# ntfy, self-hosted on the LAN or on a server you choose
ALERT_URL=http://ntfy.lan/vpn-alerts
# or any webhook that takes JSON
ALERT_URL=http://homeassistant.lan:8123/api/webhook/vpn
ALERT_FORMAT=webhook
# optional: sent as "Authorization: Bearer ..."
ALERT_TOKEN=
```

- `ntfy` (default format): a plain-text POST with `Title`, `Priority` (`urgent` for "down") and
  `Tags` headers. `webhook`: `{"event": "down|recovered|blind|sighted", "title", "message",
  "timestamp", "urgent"}`.
- **A message holds** the state, the time (UTC) and the reason, e.g. `VPN is down / Reason: the
  VPN is not running / All torrents were paused`, and `VPN is back / The VPN is running again
  after 42 s / Torrents: ...`. **Never** a password, key or IP address.
- **One alert per outage**, not one per second: a blip below the threshold sends nothing, a flap
  while still down does not repeat "down". With auto-resume the "back" message comes when the
  torrents were resumed (after the stable period), so it is accurate.
- Sending never delays a VPN poll: messages go through a queue and a worker thread, retried
  after 2, 10 and 30 s, then dropped with an error in the log. The URL is treated as a secret:
  the log names only its host.
- The alert goes out over the **home connection, not the VPN** (the watchdog is on the plain
  bridge network, like the rest of the stack except qBittorrent). With a target on the LAN
  nothing leaves the house.

**Open question for the owner:** where should alerts go? Self-hosting ntfy on the LAN keeps
everything at home but you only see alerts while on the home network (or via your own VPN to it).
A hosted service (ntfy.sh or another) reaches the phone anywhere, but the messages then pass
through a third party; they contain no secrets or addresses, but they do tell that your VPN was
down at a given time. Nothing is preset; set `ALERT_URL` when decided.

## Status for the app (the UI badge)

The watchdog serves a tiny read-only page, `GET http://watchdog:8090/status`, on the stack's
bridge network (no published port; `WATCHDOG_STATUS_PORT=0` turns it off). It returns
`{"state": "ok|down|blind|starting", "since", "reason", "paused", "last_poll", "auto_resume"}`:
no secrets, no addresses, so it needs no login. The app does not share a volume with the watchdog
and the watchdog does not use the app's database: the app only LOOKS, through
`GET /api/vpn` (behind the proxy login), which answers `state: unknown` when the watchdog does
not answer. `/health` does not depend on it.

| `state` | badge |
|---|---|
| `ok` | green: the VPN runs |
| `down` | red: the VPN is down, torrents paused (`paused`) |
| `blind` | red: the watchdog cannot read the VPN status, nothing is protecting |
| `starting` / `unknown` | grey |

## The container

Same image as the app, started with `python -m ytp.watchdog` (standard library plus our two
small clients; it never imports the web framework or the database). Runs as `PUID:PGID`, read-only
root, no capabilities, no ports, no volumes, `no-new-privileges`. Its health check is a heartbeat
file touched on every poll, so a stuck loop shows as `unhealthy`.

## Try it on the VM

```bash
docker compose logs -f watchdog      # in one terminal
docker compose stop gluetun          # in another: after 3 s "VPN unhealthy ... pausing"; the pause
                                     # cannot be delivered (qBittorrent is down with gluetun) and is retried
docker compose start gluetun         # "VPN is running again"
```

To see an actual pause, stop only the VPN inside gluetun with its own API (needs a full-access
key, not the watchdog's) or block the tunnel; with `docker compose stop gluetun` the proof is the
log and that qBittorrent has no network at all (the killswitch, `make killswitch-test`).
