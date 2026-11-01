# YouTube (issues #17, #18, #19, #20)

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

## Download: `POST /api/youtube/download`

```bash
curl -u admin http://<vm>/api/youtube/download -H 'Content-Type: application/json' \
     -d '{"url": "https://youtu.be/dQw4w9WgXcQ", "channel": "Rick Astley", "title": "Never Gonna Give You Up"}'
```

`channel` and `title` are the names the user **confirmed** (the probe suggests them; they are
editable). They become folder and file names, so they pass the same rules as the torrent
metadata (`safe_path_component`: no `/`, `..`, control characters, at most 200 characters, NFC).

| Answer | Meaning |
|---|---|
| `201 {"id", "video_id", "type": "youtube", "status": "queued", "duplicate": false}` | queued; the download runs in the background |
| `200 {... "duplicate": true}` | this video is already queued, downloading or done: the existing job is returned |
| `422` | bad address or names (field-level errors on `["body","url"]`, `["body","channel"]`, `["body","title"]`) |
| `503` | the database is not available |

Sending a **failed** video again retries it (same job, status `queued` again, new names).

**Progress** is the job's status, as for torrents: `queued`, `downloading`, `organizing` (being
filed), `done`, or `failed` with a readable reason (private/deleted/region/age, YouTube not
reachable, not enough free space, ...). One download runs at a time, oldest first. The queue is
the database: after a restart the interrupted jobs start again from the beginning, nothing is lost.

**Where it ends up:** `<MEDIA_DIR_YOUTUBE>/<channel>/<title> [<video id>].mp4`, plus
`<title> [<id>].jpg` (the thumbnail, which Jellyfin uses as the poster) and
`<title> [<id>].info.json`, and Jellyfin is asked to refresh that library (debounced, #11). The
issue text says `/data/media/youtube/...`; the stack's real library folder is `MEDIA_DIR_YOUTUBE`
(`Youtube`) directly under `/data`.
- **Not seen half-done:** the download works in `downloads/incomplete/youtube/<id>/`, which Jellyfin
  does not read, and the files are moved (a rename) into the library only when everything is
  complete, the video last. The work folder is deleted on success and on failure; a file that is
  already in the library is never overwritten.
- **Format:** H.264 video + AAC audio in an mp4 where YouTube has them (plays everywhere, Jellyfin
  direct-plays it), at most `YOUTUBE_MAX_HEIGHT` pixels high (default 1080, `0` = no limit; a
  10-minute video is around 100 to 300 MB at 1080). Merging YouTube's separate video and audio
  streams needs **ffmpeg**, which is installed in the image.
- **`.info.json` is a summary, not yt-dlp's file:** yt-dlp's own `.info.json` contains the signed
  stream addresses with the downloader's IP address in them, which must not sit in a library that
  Jellyfin (and anyone with the share) reads. We write the harmless fields: id, title, channel,
  duration, upload date, description, tags, size.
- The download goes out the same way as the probe (through the VPN by default, see below), with
  retries for flaky connections.
- **Known risk, not yet tried for real:** yt-dlp wants a JavaScript runtime (deno) for some YouTube
  formats ("some formats may be missing" is logged without one). Listing the formats of a video
  worked without one on the dev machine (53 formats, all the H.264 video and AAC audio ones among
  them), but a full download was only run through the fakes here (no ffmpeg on the dev machine);
  `tests/system/test_c18_youtube_download.py` does it for real in the built image. If downloads
  start failing with `n challenge` or "format not available" errors, a JS runtime in the image is
  the next step.

## Subscriptions: `/api/subscriptions` (issue #19)

A subscription is a channel the app watches for new videos (the polling itself is #20). The
channel's RSS feed is keyed by the channel id (`UC...`, 24 characters), not by the `@handle`, so
the id is looked up **once**, when subscribing, and stored.

- `POST /api/subscriptions {"channel": ..., "name": ..., "backfill": 0}`. `channel` can be the
  channel id, an `@handle`, a `youtube.com/channel/UC...` or `youtube.com/@handle` address
  (optionally with a tab like `/videos`), or the address of **one video** (which is how the form
  subscribes: the channel is read from the video). Everything else is refused with a message, the
  same way as for videos (other sites, ports, logins, playlists, the legacy `/c/` and `/user/`
  names), and what yt-dlp gets is rebuilt from the parsed value. The lookup uses the same proxy and
  restricted yt-dlp as the probe (YouTube extractors only, nothing downloaded, nothing cached).
  Answers: 201 with the subscription; 200 `duplicate: true` if the channel is already subscribed
  (nothing changes, whatever address was used); 422 per field; 409 above 200 subscriptions; a failed
  lookup keeps the probe's `kind` and status (`not_found` 404 for a channel that does not exist,
  `network` 502 for YouTube/VPN problems, ...).
- `name` is the **library folder** the channel's videos will go to (default: YouTube's channel
  name, with `/` and `\` replaced by `-` and control characters dropped; the channel id if
  nothing usable is left). An explicit name goes through the same check as everywhere else.
- `backfill` (0-15, default **0**) is how many of the channel's existing videos the first poll
  should also fetch. The default means: **only videos published after subscribing; the back
  catalogue is never downloaded.** 15 is the size of a channel's RSS feed.
- `GET /api/subscriptions` lists them (by name) with `last_checked` and `last_error` (both empty
  until the poller of #20 has run; it records them with `subscriptions.record_poll`).
- `DELETE /api/subscriptions/{id}` unsubscribes (204; 404 if unknown). Videos already downloaded
  stay where they are.

## Channel polling (issue #20)

A scheduler job (`subscription-poll`, every minute) looks at the subscribed channels that are due
and queues their new videos in the same queue as `POST /api/youtube/download` (#18). Everything
after that is the normal download path: job list, retry, filing, Jellyfin refresh.

- **The feed:** `https://www.youtube.com/feeds/videos.xml?channel_id=UC...`, the 15 newest videos,
  no API key. It is fetched through the same proxy as yt-dlp (`YTDLP_PROXY`, the VPN by default;
  empty = directly), without following redirects, with a 15-second timeout and a 1 MB limit.
- **The XML is untrusted.** Any `<!DOCTYPE` or `<!ENTITY` makes the feed be refused before it is
  parsed (a real feed has neither; they are what entity-expansion attacks need), so the standard
  library parser is enough and no dependency was added. Only the video id (11 characters), the
  title and the publication time are taken; the feed must name the channel that was asked for.
  Titles are made safe as folder/file names (`/` and `\` become `-`, control characters go; the
  video id if nothing is left).
- **How often:** `SUBSCRIPTION_POLL_MINUTES` (default **60**, 15 to 1440, `0` = never poll). Each
  channel gets its own interval between 0.8 and 1.2 times that (fixed per channel, derived from
  its id), so they drift apart, and the run pauses 3 to 8 seconds between two channels and takes at
  most 20 per minute. After an error a channel is tried again after 10 minutes instead.
- **First subscribe:** only videos published **after** subscribing are downloaded; the back
  catalogue never is. `backfill` (see Subscriptions above) is the exception, for the first poll
  only. A video published before the subscription never counts, however late it shows up.
- **Which videos are "seen":** the ones the queue already has a job for, in any state. The unique
  index of `jobs` decides, so a video you downloaded by hand, or that two channels both list, is
  fetched once; and a video that **failed is not queued again** by every poll: it sits in the job
  list with its reason and a **Try again** button. This replaces yt-dlp's `--download-archive`
  from the issue text: the queue already is the record, an archive file would be a second one that
  can disagree with it (a failed download is not in the archive, so it would be re-attempted each
  hour; a manual download is not in it either), and there is nothing extra to back up or clean up
  when a channel is removed.
- **One channel failing does not stop the others.** The reason goes to the subscription's
  `last_error` (the list shows it) and `last_checked` is set either way. If the feed cannot be
  reached at all (VPN down) the run stops after that first failure instead of trying every
  channel, and the others stay due.
- **One Jellyfin refresh per batch:** the poller wakes the download worker once at the end; the
  library refresh is debounced by `LibraryRefresher` (a batch of videos finishing together causes
  one refresh per library after things have been quiet for a while).
- **Live streams and premieres** are listed in the feed like videos. Until they have ended
  yt-dlp refuses them, so such a job fails with "This is a live stream or premiere that has not
  ended yet"; the same video can be retried from the job list later.

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
