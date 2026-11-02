# Dashboard and alerts (issue #38)

Two things on top of the input page: a **strip of alerts** under the VPN bar, and a page,
`/dashboard`, that shows what is going on. Both are the same web page (`index.html`); the script
shows the view that matches the address. A link in the page header switches between them.

## The alert strip

Under the VPN bar: at most the three newest **unread** alerts as small chips (red for errors,
amber for warnings, green for notices). Each chip

- is a link to the alert's detail on the dashboard (`/dashboard#alert-12`), where it is scrolled
  into view and framed;
- has a **×** that marks it as read (it disappears from the strip, it stays in the log).

"Mark all as read" appears when two or more are unread. The strip is hidden when nothing is
unread. It polls every 5 seconds with the VPN bar (not while the tab is hidden).

## The dashboard

| Part | What it shows |
|---|---|
| Running and waiting | every queued, downloading or organizing job of every kind (torrent, YouTube, podcast) with percent, downloaded of total, speed and remaining time. Refreshed every 3 seconds while the page is open. |
| Recently finished or failed | the 20 newest, with the reason for a failure and the same **Try again** as the job list (`POST /api/jobs/{id}/retry`). |
| VPN and watchdog | the current state and the history of changes (time, state, "torrents paused"). |
| Storage | free space on the media drive. |
| Alerts | the whole log (newest first, 50), each with *what happened*, *what the app did* and *what you can do*, in German or English; unread ones are highlighted and can be marked as read. |

Numbers that are not known yet are left out (a queued job has no speed, a magnet link has no
size until its file list is loaded). qBittorrent's "infinite" remaining time is left out as well.

## Where alerts come from

The **alert monitor** (`app/ytp/monitor.py`) is a scheduled job of the app, every 15 seconds. It
only reads and writes the app's own tables; it controls nothing.

| Kind | Severity | When |
|---|---|---|
| `vpn_down` | error | the watchdog reports the VPN as down |
| `vpn_blind` | error | the watchdog cannot read the VPN status |
| `egress_mismatch` | error | the egress check says the traffic does not leave through the VPN |
| `watchdog_silent` | warning | the watchdog has not answered for 4 polls in a row (one missed poll is not news) |
| `vpn_recovered` | info | back to `ok` after one of the four above |
| `job_failed` | error | a job failed (any kind, including "no sources for 7 days" from #40). Only jobs that fail after the app started watching; old failures are not announced |
| `feed_error` | warning | a channel or podcast got a new or different `last_error`; not repeated while the same error stays |
| `disk_low` | warning / error | the media drive has less than 10 % / 5 % free; once per crossing |

The watchdog has no database and is not changed: **the app polls its status** (`/status`, as for
the VPN bar) and records the *changes*. A restart of the app does not repeat an alert (the last
state is kept in `app_state`). `starting` alone never alerts.

The separate push channel of the watchdog (ntfy or a webhook, `ALERT_URL`, #22) is unchanged and
independent: it works without the app and leaves the LAN only if you set it up.

### What is stored, and what never is

`alerts` holds `kind`, `severity`, `title`, `detail` (English fallback texts), `params` (names,
reasons, numbers) and the read flag. The page translates by `kind` and fills in `params`; an
unknown kind (a newer server, an older page) shows the English `title` and `detail`.

- **No address and no secret.** Every text and parameter goes through `alertlog.redact`, which
  replaces anything that looks like an IPv4 or IPv6 address; magnet links, tokens and passwords
  are never passed in. The watchdog's status carries no address to begin with.
- The log keeps at most 500 alerts, none older than 90 days. The VPN history keeps 500 changes.
- Text from outside (a feed's error, a video title) is only ever set as text in the page, never
  as markup.

## The API

All of it is behind the proxy login; the POSTs pass the same-origin check like every other
state-changing request. Details are in the module docstrings (`alerts_api.py`, `dashboard_api.py`).

```
GET  /api/alerts?limit=30&unread=1      {"alerts": [...], "unread": n}
GET  /api/alerts/{id}                   one alert (404 if pruned)
POST /api/alerts/{id}/ack               mark as read
POST /api/alerts/ack-all                mark everything as read
GET  /api/dashboard                     {"active", "recent", "vpn", "disk"}
```

## Adding an alert from other code

```python
from ytp import alertlog
alertlog.add(db, "update_failed", "error", "The update failed", "English fallback text",
             {"sha": "abc1234"}, key="update:abc1234")
```

`key` makes recording twice a no-op. Add German and English texts to `index.html`
(`alert.<kind>.title`, `.what`, `.did`, `.todo`; a test checks that both languages have the same
keys and that every placeholder has a value) and the kind to the table above.
