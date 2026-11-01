# Podcasts (issue #34)

Podcasts are a media type of their own: paste the address of a podcast's RSS feed in the form, then
pick **one episode** and/or **subscribe** so new episodes arrive by themselves. Everything ends up
in the library folder `MEDIA_DIR_PODCASTS` (default `Podcasts`), for Jellyfin.

## Using it

1. In the form, paste the feed address (`https://…/feed.xml`). Any web address that is not YouTube
   is taken for a feed; the type switch at the top can be set by hand.
2. The app reads the feed and shows the podcast's name (the **folder name**, editable) and its
   newest 50 episodes, newest first.
3. Choose an episode to download only that one, tick **Subscribe to the podcast** to get new ones
   automatically, or both. **Also take older episodes** (0 to 20, default 0): normally only
   episodes published *after* subscribing are downloaded; the back catalogue never is, unless asked.
4. Subscribed podcasts are in the **Subscriptions** list next to the YouTube channels, each with
   *Check now* and *Remove*. Removing keeps every file already downloaded.

Downloads appear in the job list like all others (label `Podcast - Episode`), with progress, the
reason when one fails, and *Try again*.

## Files

```
Podcasts/<podcast>/<YYYY-MM-DD> - <episode title>.<ext>
Podcasts/<podcast>/folder.jpg            the cover, once per podcast (or folder.png)
```

* The date is the episode's publication date (UTC); the title is made safe for a file name
  (`/` and `\` become `-`, control characters go, at most 200 characters, shortened further if the
  name would be too long for the file system). The names come from the feed's metadata, **never from
  the web server's answer** (no `Content-Disposition`).
* The extension is taken from a fixed list (`mp3 m4a aac ogg oga opus wav flac mp4 m4v webm`):
  first from the enclosure address, then from the address after redirects, then from the content
  type the server sent, then from the one the feed declared. If none fits, the job fails with "the
  file type of this episode is not recognised" instead of guessing.
* **Nothing is overwritten.** A different file with the same name gets ` (2)`, ` (3)`. If the very
  same file (same name and size) is already there, which happens when a job is interrupted right
  after filing, it is recognised and no copy is made.
* The cover is only fetched when the folder has none yet (a `folder`, `cover` or `poster` picture
  you put there yourself is respected). A cover that cannot be fetched never fails an episode.
* Episodes are downloaded to `downloads/incomplete/podcasts/<key>/` first (never visible to
  Jellyfin) and moved in when complete; a failed or interrupted download leaves nothing in the
  library. The folder name of a podcast is fixed by the **subscription**: a single episode picked
  later from a subscribed feed goes into the same folder (YouTube videos filed as podcast episodes,
  [youtube.md](youtube.md#as-a-podcast-episode-optionally-audio-only-issue-35), share it too).
* Episodes are identified by the feed's **GUID** (the enclosure address if a feed has none),
  together with the feed address. The same episode is therefore downloaded once, whether it was
  picked by hand or found by the poller, and a failed one is *not* queued again by every poll
  (use *Try again*), exactly as for YouTube.

## Jellyfin

Jellyfin has no "podcast" library type. The owner decides; the recommendation is:

* Create a Jellyfin library of the type **Music** that points at `/media/jellyfin/Podcasts` (the
  share as Jellyfin sees it, [jellyfin.md](jellyfin.md)). Each podcast then shows as an artist/album
  and each episode as a track, taken from the tags inside the files (almost every podcast tags its
  mp3s). Files without tags show up by file name.
* Do not use the *Movies* or *Shows* types (an audio file is not a film) and do not point the
  library at `/media/jellyfin` as a whole.
* **Audiobookshelf** is a real podcast and audiobook app and can read the same `Podcasts` folder;
  if listening in Jellyfin turns out to be unpleasant, that is the same open question as for
  audiobooks (see [jellyfin.md](jellyfin.md#audiobooks-open-question-for-the-owner)). Nothing is lost
  by changing: the files stay where they are.

After each finished episode the app asks Jellyfin to refresh that library (debounced, so a batch of
new episodes means one refresh).

## How feeds and files are fetched, and why that is safe

A feed is written by a stranger, and it decides which address the episode files are fetched from:
`<enclosure url="http://192.168.178.1/…">` would make a naive downloader read a LAN device. All
fetching of podcast feeds, episodes and covers therefore goes through one module,
`app/ytp/safefetch.py`, which enforces:

* **Address syntax:** `http` or `https` only, no `user:pass@`, ports 80, 443, 8080, 8443 only, at
  most 2000 characters, plain host names or IP addresses.
* **Public addresses only.** The name is resolved by the app and *every* address must be a global
  one: loopback, private ranges (so the home LAN, `192.168.178.0/24` included, and the Docker
  network), link-local (`169.254.x.x`, cloud metadata), carrier-grade NAT, multicast, reserved and
  documentation ranges are refused, and so are IPv6 forms that wrap an IPv4 address. One private
  address among public ones refuses the whole name. IP literals in any spelling (`127.1`,
  `2130706433`, `0x7f.1`, `[::1]`) are judged by what they resolve to.
* **The address that was checked is the address connected to.** The app connects to the IP it
  checked and uses the real name only for the TLS handshake and the `Host` header, so a DNS answer
  that changes between the check and the connection (DNS rebinding) changes nothing.
* **Through the VPN proxy it is the same.** `PODCAST_PROXY` (default gluetun's HTTP proxy,
  `http://gluetun:8888`) is given the IP, never the name: `CONNECT 93.184.216.34:443` for https and
  an absolute-URI request to the IP for http. This matters because gluetun can reach the home LAN
  (`FIREWALL_OUTBOUND_SUBNETS`): a proxy that did its own DNS lookup could be sent there by a name
  that answers a public address first and a private one later.
* **Every redirect is checked from scratch** (at most 5): a feed or a tracking redirect cannot
  bounce the app to a private address.
* **Limits:** connect and read timeouts, an overall deadline per download (2 hours), a size limit
  (feed 5 MiB, cover 5 MiB, episode 2 GiB; checked against `Content-Length` *and* counted while
  reading), no compressed transfer (a compressed answer is refused), at most 3 feed lookups at
  once, at most 300 episodes read per feed, at most 100 subscriptions.
* **Content checks:** a feed must have a text/XML/RSS content type; an episode an audio, video or
  generic binary one; a cover a JPEG or PNG (checked by its first bytes too). An HTML or JSON page
  behind an "mp3" address is refused instead of being filed.
* **The XML** is read like the YouTube feeds (`feeds.read_xml`): UTF-8 only and any DTD or entity
  declaration refused, so entity-expansion and external-entity tricks have nothing to work with.
  Titles reach the page only as text (never as HTML), and file names only through the name check.
* **The browser never supplies a file address.** "Download this episode" names the episode by its
  key; the app reads the feed again and takes the address from there.

**Residual risk (accepted, documented):**

* The host name is looked up by the app's own resolver, which is outside the VPN: the home DNS
  server (or the provider's) sees *which podcast hosts* are used, not what is downloaded. Making
  that go through the VPN as well would need DNS-over-the-proxy and was judged not worth the
  complexity; the content and the connection to the host do go through the VPN.
* The IP pinned from the local lookup may differ from what the VPN exit would have been given by
  a geo-DNS CDN; it is a valid address of the same host and works, but is not "the nearest one".
* A public host that the feed points at is trusted to serve what it announces. Files are only
  stored, never executed or interpreted, but they are served by Jellyfin to your devices.
* If a legitimate podcast uses another port than the four allowed, or only compressed feeds, it is
  refused; widen the lists in `safefetch.py` knowingly if that happens.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `MEDIA_DIR_PODCASTS` | `Podcasts` | Library folder, relative to `DATA_ROOT`. Must exist (`make layout`) and be distinct from the other library folders. |
| `PODCAST_PROXY` | `http://gluetun:8888` | Where feeds and episodes go out. Empty = directly from the home line (some podcast hosts block VPN addresses). |
| `PODCAST_POLL_MINUTES` | `240` | How often each feed is checked (30 to 1440, each with its own slightly different interval; 0 = never, *Check now* still works). After an error a feed is tried again after 10 minutes. |

All settings: [configuration.md](configuration.md).

## Tests

* Unit (no network): `tests/unit/test_safefetch.py` (the address rules, every blocked range, mixed
  DNS answers, redirects into the LAN, limits, proxy requests carry the IP), `test_podcasts.py`
  (feed reading, hostile XML, keys, names, filing, covers), `test_podcast_jobs.py` (worker and
  poller), `test_podcasts_api.py` (the API, the real fetcher refusing LAN feeds end to end),
  `test_web_ui.py`.
* Real network, opt-in: `YTP_NETWORK_TESTS=1 make test-system` runs `tests/system/test_c34_podcasts.py`
  (a public feed, one real episode through a chain of tracking redirects, filed). Add
  `YTP_PODCAST_PROXY=http://127.0.0.1:8888` to send it through gluetun's proxy on the VM.

## Not included (ideas for later)

OPML import, feeds that need a login, Atom-only "podcasts", episodes without a publication date
(skipped), chapters/transcripts, tag rewriting of downloaded files, deleting old episodes.
