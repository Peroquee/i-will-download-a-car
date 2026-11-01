# YouTube (issues #17, #18)

The app reads a YouTube video's metadata (and, with #18, downloads it) with **yt-dlp used as a
library**: pinned in `app/requirements.txt`, never started through a shell.

## Probe: `POST /api/youtube/probe`

```bash
curl -u admin http://<vm>/api/youtube/probe -H 'Content-Type: application/json' \
     -d '{"url": "https://youtu.be/dQw4w9WgXcQ"}'
```

Returns `video_id`, `url`, `title`, `channel`, `channel_id`, `duration` (seconds), `duration_text`
and `thumbnail`. Nothing is downloaded or stored.

| Answer | Meaning |
|---|---|
| `200` | the metadata |
| `422` | the text is not a single-video YouTube address (field-level error on `["body","url"]`) |
| `404` `{"kind": "private"\|"unavailable"}` | private, or deleted/removed |
| `403` `{"kind": "region"\|"age"\|"members"}` | blocked in this country, age-restricted, members-only |
| `409` `{"kind": "live"}` | a live stream or premiere that has not ended |
| `502` `{"kind": "bot_check"\|"network"\|"unknown"}` | YouTube asked for proof of being human, could not be reached, or something unexpected |
| `504` `{"kind": "timeout"}` | no answer within `YOUTUBE_PROBE_TIMEOUT` seconds (default 25) |
| `429` `{"kind": "busy"}` | more than two probes at the same time |

Every failure has a stable `kind` (for the UI to translate) and a readable English `detail`.

## What is accepted, and why it is safe

- Only **one video** on `youtube.com` (also `www.`, `m.`, `music.`) or `youtu.be`: `watch?v=`,
  `/shorts/`, `/embed/`, `/live/`, `youtu.be/ID`. Any other site, a login in the address, a port,
  a playlist, a channel or a `@handle` is refused with a 422 before yt-dlp sees anything.
- yt-dlp is not given the pasted text. It gets `https://www.youtube.com/watch?v=<id>` rebuilt
  from the extracted 11-character id, so a `list=` parameter, a start time or any tail is gone.
  **No playlist expansion** (`noplaylist`).
- Options are fixed in code (`ydl_options`, pinned by tests): metadata only, YouTube extractors
  only, no cache, no config files, no retries, short socket timeouts, no `file://` URLs.
- A probe never hangs a request: it runs in a worker thread, the request waits at most
  `YOUTUBE_PROBE_TIMEOUT`, and at most two probes run at once.

## Through the VPN or not (recommendation; the owner decides)

By default yt-dlp asks YouTube **through the VPN**: `YTDLP_PROXY=http://gluetun:8888` is gluetun's
own HTTP proxy, switched on with `HTTPPROXY=on` (URL logging off). It listens inside the stack's
bridge network only and is not published. If the VPN is down, the proxy fails and the probe says
"YouTube could not be reached"; nothing goes out on the home line.

Recommendation: keep the VPN route, because it costs one setting and keeps the home address out
of YouTube's logs. The price: YouTube sometimes distrusts VPN addresses and answers "confirm you're
not a bot" (kind `bot_check`). If that happens too often, put **`YTDLP_PROXY=`** (empty) in `.env`
and `docker compose up -d`: yt-dlp then asks directly from the home line.

## Keeping yt-dlp current

YouTube changes its pages often and yt-dlp releases fixes within days. A probe that suddenly fails
with kind `unknown` for every video usually means the pinned version is stale: bump
`yt-dlp==...` in `app/requirements.txt`, run the tests, rebuild (`docker compose build app`).
The update cadence belongs to #30.

## Not covered here

The thumbnail is a YouTube address (`i.ytimg.com`): a browser that shows it contacts YouTube.
Whether the form shows it that way or the app proxies it is a decision for the form (#25).
