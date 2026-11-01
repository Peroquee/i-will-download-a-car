# Jellyfin (external) (issue #7)

Jellyfin is **not** part of this stack. It runs in its own privileged LXC (Proxmox id 100,
`192.168.178.108`) and keeps running there. This stack only tells it to refresh a library
after a download, through its API. The `jellyfin` service and `JELLYFIN_TAG` were removed from
`docker-compose.yml` and `.env.example`; there is nothing to start or update here.

## What the app needs

Two settings in `.env` on the VM (the app gets both; the compose file does not require them
yet because the app is still a placeholder):

| Setting | Value |
|---|---|
| `JELLYFIN_URL` | Jellyfin's base address, e.g. `http://192.168.178.108:8096`. **The port is not confirmed yet** (8096 is Jellyfin's default); check it in the LXC (`ss -ltn` there) or in Dashboard > Networking before relying on it. |
| `JELLYFIN_API_KEY` | A secret you create in Jellyfin, below. Never in Git, never in chat. |

## Create the API key

1. In a browser open Jellyfin and sign in as an administrator.
2. **Dashboard > Advanced > API Keys** (older versions: *Dashboard > API Keys*), click **+**.
3. Name it `youtube-premium` and confirm. The key appears in the list.
4. Copy it into `.env` on the VM, with an editor, not on a command line:

```bash
nano .env      # JELLYFIN_API_KEY=<paste the key>, Ctrl+O, Ctrl+X
chmod 600 .env
```

To revoke it later, delete the entry in the same list. The app then gets "unauthorized"
and shows that in its job list instead of failing silently.

## How the libraries relate to this stack

- **Jellyfin manages its libraries itself** (Dashboard > Libraries). They already point at the
  media subfolders: `Filme`, `Serien`, `Hörbücher`, `Musik`, `Youtube`. Nothing in this repo
  creates or changes a library.
- **A library must never point at `/media/jellyfin` as a whole.** That share also holds
  `downloads/` (unfinished and seeding torrents), which would then show up in Jellyfin.
  Always use the subfolders.
- **Two views of the same folders.** The app and qBittorrent see the share as `/data/<dir>`;
  Jellyfin sees it as `/media/jellyfin/<dir>`. The app asks Jellyfin to refresh a library (or
  runs a full refresh) rather than passing file paths, so no path translation is needed. If a
  later feature ever has to give Jellyfin a path, it must translate `/data` to `/media/jellyfin`.
- **Permissions.** Jellyfin runs as uid 103 / gid 112 and reads through the "other" permission
  bits; the new files hardlinked by the app inherit mode 664/775 from `UMASK=002`, which keeps
  them readable.

## Audiobooks: open question for the owner

The library folder `Hörbücher` exists, but Jellyfin has no first-class audiobook support. Two
options; **not decided, nothing implemented**:

| | Jellyfin with the Bookshelf plugin | Audiobookshelf on the same folder |
|---|---|---|
| Effort | one plugin in the existing Jellyfin | one more container (this stack or the LXC) |
| Fit | everything in one app and one login | built for audiobooks and podcasts: progress sync, chapters, mobile apps |
| Risk | third-party plugin, less polished for audiobooks | one more service to run and update |
| Needs from this repo | nothing, the app refreshes the library | a refresh call to Audiobookshelf's API (another setting and client) |

Recommendation if the owner has no preference: start with Jellyfin and the plugin (no new
service); switch to Audiobookshelf if the listening experience is not good enough, since both
read the same `Hörbücher` folder and nothing is lost by changing.
