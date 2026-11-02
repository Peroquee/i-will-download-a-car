# Operations: backups, updates, log rotation (issue #30)

Nothing here is switched on for you. The scripts are safe to re-run, support `--dry-run`, and
the schedules below are **snippets for the owner to install**: the repository never touches the
host's crontab or systemd.

## What is lost if the VM dies

| Thing | Where | In Git? | In the backup? |
|---|---|---|---|
| Code, compose file, scripts | the repository | yes | no (clone it again) |
| Secrets: VPN login, proxy password hash, API keys | `.env` | **never** | **yes** |
| gluetun's auth file and custom `.ovpn`, qBittorrent's settings and resume data | `config/` | **never** | **yes** |
| Jobs and subscriptions | `config/app/app.db` (SQLite, WAL) | no | **yes**, as an online copy |
| Downloads and the media libraries | `/data` on the share | no | **no** (large; protect the share itself) |
| Jellyfin | its own LXC | no | no (its own backup) |

## Backups: `scripts/ops/backup.py`

```bash
make backup DRY=1          # shows what would be backed up, writes nothing
make backup                # makes backups/ytp-backup-<time>.tar.gz
make backup-list           # lists the archives
scripts/ops/backup.py verify backups/ytp-backup-<time>.tar.gz   # complete? database sound?
```

- **The database is copied with SQLite's online backup**, so the copy is consistent while the app
  runs. A plain `cp` of a WAL database can be torn; the script never does that. The copy is
  checked (`PRAGMA integrity_check`) and its hash goes into the archive's `MANIFEST.json`.
- **`config/`** is archived without the live database files, `logs/` and `cache/` folders. Symbolic
  links inside it are skipped, never followed. `config/` itself may be a symlink.
- **`.env`** is included (a restore without it has no VPN login). That is why the archive
  **contains secrets**: it is mode 600 in a folder with mode 700, `backups/` is ignored by Git, and
  the archive carries no owner names. Never commit it, never put it on a share others can read.
- **Rotation:** the newest 14 archives are kept (`--keep N`); older ones of the script's own name
  pattern are removed after a successful run, nothing else in the folder is touched.
- **Failing loudly:** if a file cannot be read (run it as the owner of `config/`, the stack's
  PUID:PGID, or with `sudo`), if `.env` or the database is missing, or if the finished archive does
  not pass its own check, the script exits with an error and keeps **no** incomplete archive.
- Every run writes under a temporary name and renames when finished, so a crash never leaves a
  half archive that looks real.

### Where the backups go (an open question for the owner)

The default is `backups/` inside the checkout. **That is on the same disk as everything else and
does not survive losing it.** Choose one or more of:

1. `YTP_BACKUP_DIR=/mnt/nas/ytp-backups` (or `--target`) pointing at a mounted NAS share, ideally
   one that only you can read; the archive is mode 600 but the share's own rules apply.
2. A Proxmox backup of the whole VM (`vzdump`, or a ZFS snapshot of its disk) on the node's
   schedule: it covers `.env`, `config/` and the database in one go, but is crash-consistent only.
   This is a good second layer next to the archives, not a replacement for them.
3. Copying the newest archive off site; encrypt it first, for example with
   `age -p -o file.age file` or `gpg -c file`. The script does not encrypt, so the choice of tool
   and key handling stays with you.

### Scheduling it (snippets to install yourself)

systemd timer (as root: `systemctl edit --force --full ytp-backup.service` and the same for the
timer, then `systemctl enable --now ytp-backup.timer`). Replace the user and the folder:

```ini
# /etc/systemd/system/ytp-backup.service
[Unit]
Description=youtube-premium backup

[Service]
Type=oneshot
# the owner of config/ (the stack's PUID:PGID), so every file can be read
User=media
WorkingDirectory=/opt/youtube-premium
# optional: a mounted share instead of ./backups
# Environment=YTP_BACKUP_DIR=/mnt/nas/ytp-backups
ExecStart=/opt/youtube-premium/scripts/ops/backup.py

# /etc/systemd/system/ytp-backup.timer
[Unit]
Description=youtube-premium backup, daily

[Timer]
OnCalendar=*-*-* 03:30:00
# run once after boot if the VM was off at 03:30
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
```

Or, if you prefer cron (`crontab -e` as that user; the line runs daily at 03:30 and keeps the output):

```cron
30 3 * * * cd /opt/youtube-premium && scripts/ops/backup.py >> backups/backup.log 2>&1
```

Look at `systemctl list-timers` / `journalctl -u ytp-backup` (or `backups/backup.log`) now and
then, and run `scripts/ops/backup.py verify` on a recent archive once in a while: a backup that
was never read back is a hope, not a backup.

### Restoring

`restore` only ever unpacks into an **empty folder**; putting the files in place is your decision.
On a rebuilt VM (host setup done as in [host-setup.md](host-setup.md), repository cloned,
stack not running):

```bash
# 1. unpack the archive into a scratch folder (checks every member and the database first)
scripts/ops/backup.py restore /path/to/ytp-backup-<time>.tar.gz --into ~/restore
# 2. the secrets: put .env back, private
install -m 600 ~/restore/env/dotenv .env
# 3. the configuration (gluetun auth + .ovpn, qBittorrent settings and resume data)
mkdir -p config && cp -a ~/restore/config/. config/
# 4. the database, then let the layout script give it to the app user
mkdir -p config/app && cp ~/restore/db/app.db config/app/app.db
sudo scripts/host/create-layout.sh
# 5. start and check
docker compose up -d
make proxy-check
# 6. delete the scratch folder: it holds the secrets again
rm -rf ~/restore
```

The jobs and subscriptions come back as they were. Downloads that were in progress start from the
beginning (the app requeues interrupted jobs at start); finished media is on the share and was
never part of the backup. The full-stack recovery test (#32) exercises this path.

## Full-stack recovery test: reboot and check (issue #32)

After a power cut or a Proxmox update the whole chain has to come back **by itself and in the
right order**: Proxmox starts the VM, Docker starts, gluetun and the VPN come up first,
qBittorrent only after the VPN is healthy, the app before the proxy, and the jobs that were
running either carry on or fail with a reason. This is the test for that. It is yours to run
(it needs a reboot, which no script on the VM can do for itself), and the owner decides when.

**Before the first run, once, on the Proxmox node** (a script cannot check this from inside the VM):

```bash
qm config 210 | grep -E 'onboot|startup'     # expect: onboot: 1  and  startup: order=1
```

The VM then starts with the host. On the VM, Docker must start at boot (`systemctl is-enabled
docker` says `enabled`; `verify` checks it) and every service has `restart: unless-stopped`
(checked per container).

```bash
# 1. on the VM, stack up and healthy, with a download in progress (add a small legal torrent
#    or a YouTube video so there is a job to judge):
make recovery-prepare
# 2. reboot: the whole Proxmox host (the real test) or only the VM (a quicker, weaker one):
#      on the Proxmox node:  reboot          on the VM:  sudo reboot
# 3. when you can log in again, on the VM:
make recovery-verify
```

`recovery-prepare` writes `reports/recovery-state.json` (mode 600; container start times, the boot
id, the in-progress job ids and states, no names or links). `recovery-verify` waits up to 10 minutes
for the stack (`--wait`) and prints one line per check, never an address:

| Check | Passes when |
|---|---|
| reboot | the boot id changed (`--no-reboot-check` for a test that only restarts containers) |
| every service | running again, **started after** the reboot, healthy, with a restart policy that brings it back |
| order | gluetun started before qbittorrent, app before proxy, and qbittorrent was started only after gluetun's log said its tunnel was up (`[openvpn] Initialization Sequence Completed`) and its DNS ready (`[dns] ready`), since gluetun never logs \"healthy\" (a WARN if those lines cannot be read, for example with WireGuard: then only the start order is judged) |
| VPN before traffic | the watchdog says `ok`; qBittorrent's public address is a VPN one, not the home one (`VPN IP`, never `HOME IP LEAK`); no torrent is running while the VPN is not ok |
| jobs | each job that was in progress is `done`, `failed` **with a reason**, or on its way again (`queued`/`downloading`); one stuck in `organizing` for over 10 minutes, one that failed without a reason or one that vanished is a failure; with no job in progress it WARNs (`--require-jobs` makes that a failure) |

Exit code 0 = passed, 1 = a check failed, 2 = it could not run (stack not healthy at `prepare`, no
state file). What the app does by itself after a restart: YouTube jobs that were `downloading` or
`organizing` go back to `queued` and start again, and the reconciler fails a torrent job whose
torrent qBittorrent no longer has, with that reason.

What this does **not** prove: that no packet left the VM outside the VPN during the first seconds
of boot (the killswitch is gluetun's firewall plus qBittorrent sharing its network, which cannot
exist before gluetun does; [killswitch.md](killswitch.md) proves that part separately), and that the
VM starts with the host (the `qm config` check above).

## Updates

Everything is **pinned and updated on purpose**. There is no Watchtower: it needs the Docker
socket (which this stack never mounts) and would update the VPN gateway or qBittorrent while you
sleep, which is how a killswitch quietly stops working.

| What | Pinned in | Suggested cadence | Check |
|---|---|---|---|
| yt-dlp | `app/requirements.txt` | weekly look, update when YouTube breaks | `make update-check` (exit 10 = newer exists) |
| gluetun | `GLUETUN_TAG` in `.env` | every few months; read its release notes | docker hub `qmcgaw/gluetun` tags |
| qBittorrent | `QBITTORRENT_TAG` in `.env` | every few months or for a security fix | `lscr.io/linuxserver/qbittorrent` tags |
| Caddy | `CADDY_TAG` in `.env` | for security fixes | docker hub `caddy` tags |
| App base image, Python packages | `app/Dockerfile`, `app/requirements.txt` | when a security fix matters | rebuild: `docker compose build --pull app` |

`QBITTORRENT_TAG` used to be `latest`. It is now the exact build that runs on the media VM and
passes the system tests (`5.3.0_v2.1.2-ls485`). **On the VM, set the same value in `.env`** (the
tests read `.env.example`; compose reads `.env`), then `docker compose up -d qbittorrent`. The
unit tests refuse `latest`/`stable`/`edge` in `.env.example`.

### yt-dlp: the one that breaks

YouTube changes its pages often and yt-dlp releases fixes within days. When every probe or
download fails with `unknown`/`bot_check` kinds, or `make update-check` says a newer release
exists:

```bash
scripts/ops/update_ytdlp.py --apply --dry-run   # shows the one-line change
scripts/ops/update_ytdlp.py --apply             # rewrites the pin in app/requirements.txt
make test-unit                                   # the tests that pin yt-dlp's options and error texts
YTP_NETWORK_TESTS=1 make test-system             # real YouTube: probe, download, channels, feed
git add app/requirements.txt && git commit -m "yt-dlp <version>"   # then the usual MR
docker compose build app && docker compose up -d app watchdog      # the watchdog uses the same image
```

To roll back, put the old line back and rebuild. A fully automatic bump-and-deploy is deliberately
not set up: an update that breaks the options the app relies on would be live before anyone saw
the tests fail. The script only reports and edits one line.

### Any image tag

1. Back up first: `make backup`.
2. Change the one tag in `.env` (and in `.env.example` by merge request, so the tests use it).
3. `docker compose pull <service> && docker compose up -d <service>`.
4. Prove the guarantees that matter: `make killswitch-test` (VPN), `make proxy-check` (login),
   `make test-system` (everything).
5. Roll back by restoring the old tag and running step 3 again.

Re-check the gluetun server-list note in `.env.example` whenever `GLUETUN_TAG` changes.

## Log rotation

Every container logs to Docker's `json-file` driver capped at **10 MB x 3 files** (about 30 MB per
service at most; `docker-compose.yml`, enforced for every service by a unit test). Nothing writes
log files on disk: the app logs one JSON line per event to stdout, never secrets or addresses.

```bash
docker compose logs --since 1h app          # the last hour of the app
docker compose logs -f gluetun watchdog     # follow the VPN pair
```

Logs are kept only across restarts of a container, not after `docker compose down` followed by
`up` re-creates it. If you want them longer, the place for that is Docker's daemon-wide
`log-driver` setting or a log shipper, which this stack does not set up.
