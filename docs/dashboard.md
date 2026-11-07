# Dashboard and alerts (issue #38)

Two things on top of the input page: a **strip of alerts** under the problem banner, and a page,
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

`/dashboard` has four tabs (#62): **Overview**, **Jobs**, **VPN** and **Alerts**. The address
selects one (`/dashboard#jobs`, `#vpn`, `#alerts`, `#alert-12`); the VPN indicator links straight
to the VPN tab, and an alert link opens the Alerts tab.

### Overview

- **Three status cards** side by side, each with a coloured dot and a large state:
  - **Internet** — the app's own DNS + TCP reachability check (`ok` is *Online*, `offline` is
    *Offline*, anything else is *Unknown*), with when it was last checked. It runs on the
    app's normal egress path and is cached for 30 seconds, so it is independent of the VPN and
    of the watchdog (#63).
  - **VPN** — `ok`, `down`, `blind`, `starting` or `unknown` from the watchdog; an egress
    mismatch still shows *Not safe*.
  - **Watchdog** — whether the watchdog answers and its last poll is fresh: *Watching* (`ok`),
    *Blind*, *Stale*, *Not answering* (`unreachable`) or *Starting*, with when it last polled.
    A watchdog that answers "down" is healthy (the VPN card shows the down state), and an
    unreachable watchdog is visibly different from an offline internet line.
- **Alerts** summary (admin only): one little dot per severity with the unread count
  (error/warning/notice) and the unread total; it links to the Alerts tab.
- **Jobs** summary: the job expected to finish first (label and remaining time), the number of
  active jobs, the longest remaining time and the sum of the current speeds; it links to Jobs.
- The status rail has a fixed shape: a normal page load only changes its colours, never its
  geometry, so the page does not shift while the VPN answer arrives.

### Jobs

- **Running and waiting:** every queued, downloading or organizing job of every kind (torrent,
  YouTube, podcast) with percent, downloaded of total, speed and remaining time.
- **Whole back catalogue** (#45): the podcast runs described below.
- **Finished downloads:** the 10 newest done/failed jobs by default; **Show 50** expands to the
  newest 50. A failed job shows its reason and the same **Try again** as the job list
  (`POST /api/jobs/{id}/retry`). A failed job also has **Dismiss** (*Ausblenden*), and **Dismiss
  all failed** sits above the list. A dismissed job no longer counts in the red "failed: N" on
  the input page and leaves this list; **Show dismissed (N)** brings it back, with **Show again**
  per job. Dismissing is not deleting: the job stays and can still be retried, and a retry clears
  the dismissal (a job that fails again shows again). It is per account, also for an admin: a
  user dismisses only their own jobs, an admin any, and nobody else's view changes
  (`POST/DELETE /api/jobs/{id}/dismiss`, `POST /api/jobs/dismiss-failed`).
- Numbers that are not known yet are left out (a queued job has no speed, a magnet link has no
  size until its file list is loaded). qBittorrent's "infinite" remaining time is left out too.

### VPN

The current VPN state, the history of changes (time, state, "torrents paused"), the free space on
the media drive and the automatic-update status (the last two are the owner's only).

### Alerts

The whole log (newest first, 50), each with *what happened*, *what the app did* and *what you can
do*, in German or English; unread ones are highlighted and can be marked as read.

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
| `youtube_unavailable` | warning | channels fail because of YouTube as a whole (the channel list from yt-dlp **and** the RSS feed failed, `feeds.SERVICE_WIDE`): ONE alert for all channels, not one each; again only after it was gone and came back |
| `disk_low` | warning / error | the media drive has less than 10 % / 5 % free; once per crossing |
| `backlog_paused` | warning | "All episodes" of a podcast (#45) stopped queuing because the next episode would leave too little room; once per pause, written by `podcast_backlog.py` (not by the monitor) |

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

## Back catalogue of a podcast (#45)

"All episodes" in the podcast form starts a run that the dashboard follows: a *Whole back
catalogue* section appears (hidden when there is none) with "Back catalogue: 40 of 312", a bar, the
number not yet queued, failed and skipped, the state (running / paused for lack of free space /
finished / stopped) and a *Stop* button. `GET /api/dashboard` carries it as `backlogs` (own runs;
an admin sees all). The pause also raises the alert `backlog_paused`. Details:
[podcasts.md](podcasts.md#the-whole-back-catalogue-issue-45).

## The API

All of it needs a login (an account of the app, see [accounts.md](accounts.md); the alert routes are for admins only); the POSTs pass the same-origin check like every other
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
