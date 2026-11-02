# Automatic updates (issue #42)

The media VM can install a new version of youtube-premium **by itself**: a timer looks every five
minutes whether `main` in GitLab has a new commit, and installs it only if it is safe. If the
update fails, it goes back to the version that worked. You can pause it, see what it did on the
dashboard, and roll back by hand.

It is **off until you switch it on** (the installer asks, or see "Switching it on by hand").

## Why the VM pulls instead of GitLab pushing

A CI job that logs in to the VM would need a shell credential for the media VM stored in GitLab or
on the shared runner. The other way round, the VM only needs to **read**: one project access token
that can read the repository and the pipelines and change nothing. Nothing connects to the VM
from outside, and a leaked token can show source code and pipeline states, not run anything.

## What has to be true before it installs a commit

All of these, every time (it just waits or says why if one is not):

| Check | If not |
|---|---|
| there is a new commit on `main`, and it continues the installed one (a fast-forward) | a history that was rewritten is **not followed** (alert) |
| GitLab's pipeline for exactly that commit is **green** | running or not there yet: it waits. Red, canceled, skipped: no update, one alert per commit |
| the stack is healthy right now | no update on top of a problem (alert) |
| the new `.env.example` has **no new required setting without a default** (a value `changeme`) | no update; the alert names the setting. Fill it in `.env`, and the next run goes ahead |
| the install folder has no local changes to tracked files | no update (alert); your edits are never overwritten |
| a full backup (`backup.py create`) and a verified snapshot of the database succeed | no update, nothing changed |
| not paused, not halted, no other run in progress (a lock) | waits |

The commit that failed earlier is **not tried again** until a newer commit exists.

## What an update does

The path of `./update.sh`: backup, `git merge --ff-only`, new defaults of `.env.example` into
`.env` (nothing you set is touched), the VPN file again if you use a custom `.ovpn`,
`docker compose up -d --build`, wait until every service is healthy (up to
`AUTODEPLOY_HEALTH_WAIT`, 600 s), then the host checks. A failing host check is **reported** (alert
"host checks report problems") but does not undo a healthy stack. The result is an alert "Updated
to abc123" and a line on the dashboard.

## When an update fails: the automatic rollback

If the stack does not become healthy (or the build fails), the previous commit is put back:

1. **Code only**: no new migration file, or the database does not have one of the new migrations
   yet (the new version never got to migrate): `git reset --hard <previous>`, rebuild, start,
   wait. The database is not touched.
2. **With a migration the database already has:** the app refuses a database that is newer than
   itself, so the old code cannot run on the new schema. The app is stopped, the failed database
   is kept as `config/app/app.db.failed-<sha>-<time>` (mode 600, deleted after 14 days), the
   snapshot from just before the update is put back (stale `-wal`/`-shm` files removed), the
   code is rolled back, and the stack started. Job changes made in the few minutes in between
   are lost; that is safe, because every job is keyed by infohash, video id or episode key
   (sending it again does the same thing) and downloaded files stay on disk.
3. **If the rollback fails too** (the old version does not become healthy, a snapshot is
   missing, docker refuses): the app is **left stopped** (never a half state), the state becomes
   `halted`, an alert says so, and **nothing is tried again** until you run `resume`. See "When
   everything stands still".

The alert names the commit and whether the database was put back.

## Where you see it

* **Alerts** (strip and `/dashboard`, [dashboard.md](dashboard.md)): `update_done` (info),
  `update_blocked` (warning), `update_rolled_back` and `update_rolled_back_db` (error),
  `update_halted` (error), `update_checks` (warning).
* **Dashboard**: a line "Automatic updates: version abc123… · last run: …", or "paused" or
  "STOPPED after a failed update".
* `python3 scripts/ops/autodeploy.py status` in the install folder: installed version, paused or
  not, last run, the last five updates.
* `journalctl -u youtube-premium-autodeploy` for the log of each run.

## Switching it on

### 1. Make the token (in GitLab, once)

Project → **Settings → Access tokens → Add new token**: a name like `autodeploy-vm`, role
**Reporter**, scopes **`read_api`** and **`read_repository`**, an expiry date (a year is fine; put
it in your calendar: an expired token shows up as "GitLab refused the token"). Copy the token
once, it is not shown again. Nothing else: no write scope.

### 2. The installer

`sudo ./install.sh` (also "change settings" on an existing installation) asks "Switch on
automatic updates?", for the repository address (`http://gitlab.lan/group/project.git`) and the
token (typed hidden). It stores the token in `config/autodeploy/token` (folder 700, file 600),
installs and starts the timer, and runs `check`. For a run without questions the answers file
has `autodeploy_remote` and `autodeploy_token`.

### By hand

```bash
cd ~/youtube-premium
# the repository to watch (no login in the address), in .env
echo 'AUTODEPLOY_REMOTE=http://gitlab.lan/group/project.git' >> .env
# the token: read without echo, so it is not in your shell history
mkdir -p config/autodeploy && chmod 700 config/autodeploy
read -rs -p 'token: ' T && echo "$T" > config/autodeploy/token && unset T
chmod 600 config/autodeploy/token
# can this machine do it? (token, GitLab, git, docker, backup folder)
python3 scripts/ops/autodeploy.py check
# install and start the timer (needs root); NAME is the user who owns this folder
sudo python3 scripts/ops/autodeploy.py install-units --user NAME
```

The user needs to be allowed to use docker (group `docker`). `check` says if not.

### Switching it off

`sudo python3 scripts/ops/autodeploy.py remove-units`, or "change settings" in the installer and
answer no. To only stop for a while: `pause`.

## Day to day

```bash
python3 scripts/ops/autodeploy.py status            # what is installed, last result
python3 scripts/ops/autodeploy.py pause --reason "holiday"   # no updates until resumed
python3 scripts/ops/autodeploy.py resume            # on again (also clears "halted")
python3 scripts/ops/autodeploy.py check             # is everything in place?
python3 scripts/ops/autodeploy.py run               # what the timer does, once, now
```

`pause` is a flag file (`config/autodeploy/paused`); a pause survives reboots.

## Rolling back by hand

```bash
python3 scripts/ops/autodeploy.py rollback
```

Goes back to the previous version with the same procedure as the automatic rollback (database
snapshot included if the update migrated it), **pauses** updates and remembers the version you
left so it is not installed again. Look at what was wrong, then `resume`. If the database moved on
and there is no snapshot from just before that update, it refuses and says so: restore a backup
by hand ([operations.md](operations.md)).

## When everything stands still

You got "The update failed and the app is stopped". Nothing runs by itself now. Over SSH:

```bash
cd ~/youtube-premium
python3 scripts/ops/autodeploy.py status           # what it thinks, which version came before
docker compose ps                                    # what runs
docker compose logs --tail=100 app                   # why the app does not start
git log --oneline -5                                 # which version the folder is at
```

To go back to the version before by hand (replace `PREVIOUS` with its full sha from `status`):

```bash
docker compose stop app                              # stop the app first
mv config/app/app.db config/app/app.db.broken       # keep the broken database
cp config/autodeploy/predeploy.db config/app/app.db  # the snapshot from before the update
rm -f config/app/app.db-wal config/app/app.db-shm    # stale write-ahead files
chmod 600 config/app/app.db                          # owner = the stack user (PUID:PGID), see host-setup.md
git reset --hard PREVIOUS                            # the code before the update
docker compose up -d --build                         # start it again
python3 scripts/ops/autodeploy.py resume             # updates on again
```

If the snapshot is missing, use the newest archive from `backup.py list` and
[operations.md](operations.md) ("Restoring"). The commit that failed stays on the "failed" list
until a newer commit exists.

## For developers: rules that make this safe

* **Migrations only add, in one release:** new tables, new nullable or defaulted columns. Dropping
  or renaming something happens in a **later** release, when no code uses it any more. Then the
  previous version can still run on the new schema, and a rollback of code needs no database
  restore in most cases. `tests/unit/test_autodeploy.py::test_migrations_only_add_in_one_release`
  fails for `DROP`, `RENAME`, `DELETE FROM` in a new migration; a deliberate exception goes into
  its allow list with a reason. A table rebuild like 0005 must keep all rows and ids (tested).
* **A new required setting needs a default** in `.env.example`. A value `changeme` means "a person
  must fill this in" and stops the automatic update until they did.
* **Health is the contract:** a service that is up but broken must fail its health check, because
  that is all the rollback sees.
* A release that should not go out by itself: do not merge it to `main` until it should, or
  `pause` first.

## Security notes

* The token is only in `config/autodeploy/token` (mode 600; a looser mode is refused). git gets it
  through `GIT_ASKPASS` (`scripts/ops/git-askpass.sh`) from the environment of the one git
  command; the API call sends it in a header. It is never on a command line, in a URL, in `.env`,
  in a log or in an alert. It is part of `config/`, so it is in the backups, which are private.
* GitLab here is on the LAN over plain `http://`, as everything else (a token on the LAN is as
  exposed as the rest of the LAN; it can only read).
* The timer runs as the user who owns the folder, not as root, with `NoNewPrivileges`.
* The script writes only to the install folder (`config/autodeploy`, `config/app`, the git
  checkout, `.env`).
* What it installs is what is on `main` with a green pipeline: reviewing and merging remains the
  gate.

## Files

| Path | What |
|---|---|
| `scripts/ops/autodeploy.py` | the script |
| `scripts/ops/git-askpass.sh` | lets git read the token from the file |
| `scripts/ops/systemd/youtube-premium-autodeploy.{service,timer}` | templates; `install-units` fills in user and folder and writes them to `/etc/systemd/system` |
| `config/autodeploy/token` | the token (600) |
| `config/autodeploy/state.json` | installed version, previous version, last result, failed commit, halted, history |
| `config/autodeploy/paused`, `lock` | pause flag, run lock |
| `config/autodeploy/predeploy.db` | the verified database snapshot of the last update |
| `config/app/autodeploy-status.json` | the small public status the dashboard shows |
| `config/app/app.db.failed-<sha>-<time>` | a database kept aside after a rollback (14 days) |

After an update changed the unit templates, run the installer once more (it refreshes them) or
`sudo python3 scripts/ops/autodeploy.py install-units --user NAME`; the timer itself cannot do
that because it does not run as root.
