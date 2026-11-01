# Host setup (issue #1, C0)

Proxmox → Debian 12 VM → Docker Compose. Docker runs in a full VM, never in an
unprivileged LXC: the killswitch puts qBittorrent into gluetun's network
namespace (`network_mode: service:gluetun`) and gives gluetun `NET_ADMIN` and
`/dev/net/tun`, which unprivileged LXC does not provide reliably.

Each step below matches one checklist item of the issue and ends with how to verify it.

## 1. Create the Debian 12 VM (4 vCPU, 4 GB RAM, 32 GB system disk)

On the Proxmox node, with a Debian 12 netinst ISO uploaded:

```bash
scripts/proxmox/create-vm.sh --vmid 210 \
  --iso local:iso/debian-12.12.0-amd64-netinst.iso \
  --storage local-lvm --bridge vmbr0 \
  --media-size 500          # optional second disk for /data, see step 4
# add --dry-run first to see the qm commands
```

The VM gets `cpu host`, no ballooning (Proxmox can't take RAM away from the VPN
stack), the QEMU guest agent, `onboot=1` and `startup order=1` so it comes up
first after a host reboot (#32).

Install Debian with only "SSH server" and "standard system utilities", then
clone this repo into the VM (e.g. `/opt/youtube-premium`).

**Verify:** `qm config 210` shows `cores: 4`, `memory: 4096`, `scsi0: ...,size=32G`.

## 2. Install Docker Engine and the Compose plugin

```bash
sudo scripts/host/bootstrap.sh --user "$USER" --dry-run   # review
sudo scripts/host/bootstrap.sh --user "$USER" [media options from step 4]
# log out and back in for the docker group
```

Installs from Docker's own apt repository (`docker-ce`, `docker-compose-plugin`),
not Debian's older `docker.io`. Idempotent: safe to rerun.

The QEMU guest agent is installed and enabled, but only started if the VM has the
Proxmox option *Options > QEMU Guest Agent = Enabled* (shut the VM down and start it
again after switching it on; a reboot from inside is not enough). Without it bootstrap logs a hint and carries on instead of
waiting for a device that does not exist.

**Verify:** `scripts/host/check-host.sh --only docker,compose`

## 3. Confirm /dev/net/tun exists and is available to the VM

Nothing to do on Proxmox for a KVM VM: the device comes from the guest kernel's
`tun` module. `bootstrap.sh` loads it now and on every boot
(`/etc/modules-load.d/tun.conf`).

**Verify:** `scripts/host/check-host.sh --only virt,tun` (also confirms we're in
a VM, not a container), and `make test-system` proves a container with
`NET_ADMIN` can actually create a tunnel.

## 4. Attach the media storage volume, set PUID/PGID and umask

The media volume is mounted at `/data`; downloads and the Jellyfin libraries
both live below it on one filesystem so hardlinks work (#5).

Pick the option that matches where the media lives:

- **New virtual disk** (created with `--media-size` in step 1, shows up as `/dev/sdb`):
  `sudo scripts/host/bootstrap.sh --user "$USER" --media-device /dev/sdb --format-ext4`
  (`--format-ext4` refuses to touch a disk that already has a filesystem.)
- **Existing disk or partition with data:** `--media-device /dev/sdX` without `--format-ext4`, or `--media-uuid <UUID>`.
- **Library that lives elsewhere (NFS export, ZFS dataset on the host):** mount it at `/data` yourself (NFS: one export containing both `downloads/` and `media/`), then run bootstrap without media options.

Ownership: `/data` is owned by `PUID:PGID` (default 1000:1000, user `media` is
created if missing) with mode `2775`, so new files inherit the group.
`UMASK=002` makes files `664` and folders `775`, so qBittorrent, the app and
Jellyfin can all work on them. Use the same PUID/PGID Jellyfin reads as.

**Verify:** `scripts/host/check-host.sh --only media` (mounted, owner, group-writable, sane values in `.env`).

## 4b. Download and library folders, proven hardlinks (issue #5)

Downloads and finished media stay in separate trees, so a half-finished file can never
show up in Jellyfin. When a torrent finishes, the app **hardlinks** it into the library
folder (the torrent keeps seeding from `downloads/`; the file exists once on disk). That
only works when both trees are on one filesystem.

The real layout (the owner's existing German folders sit directly under `/data`, there is
no `/data/media/` level; Jellyfin runs in its own LXC and its libraries point at the
library folders):

```
/data/
  downloads/            <- qBittorrent only; Jellyfin never sees it
    movies/ series/ audiobooks/ music/
    incomplete/         <- unfinished torrents, moved to the category folder when done
  Filme/ Serien/ Hörbücher/ Musik/ Youtube/    <- the library folders
```

The library folder names are settings in `.env` (relative to `DATA_ROOT`), so another setup
can use other names; the defaults are the owner's: `MEDIA_DIR_MOVIES=Filme`,
`MEDIA_DIR_SERIES=Serien`, `MEDIA_DIR_AUDIOBOOKS=Hörbücher`, `MEDIA_DIR_MUSIC=Musik`,
`MEDIA_DIR_YOUTUBE=Youtube`. Spell them exactly as on disk (`ö`, `ü` included). **An `.env`
made before this step lacks these lines:** copy them from `.env.example`, otherwise
`docker compose` stops and names the missing one.

Create what is missing (needs `sudo`, see below) and prove it works:

```bash
sudo scripts/host/create-layout.sh --dry-run   # shows what it would create
sudo scripts/host/create-layout.sh             # creates only MISSING folders: owner PUID:PGID, mode 2775
sudo scripts/host/check-host.sh --only layout  # hardlink test between downloads/ and every library folder
```

- **Existing folders are never touched**: no chmod, no chown, no recursion, contents
  untouched. Only missing folders are created (`downloads/...`, and a library folder if its
  name does not exist yet).
- **Why `sudo`:** `/data` is owned by `PUID:PGID` (1001:1001 here) and the login user is not in
  that group, so creating folders owned by 1001:1001 needs root. (Alternative: add your
  login user to the media group with `sudo usermod -aG media <user>` and log in again; the
  script then also works without `sudo` if your own ids equal PUID:PGID, which they usually
  do not. We keep `sudo`; it is needed only for this one-time step.)
- **`check-host.sh --only layout`** creates a small hidden temp file in each download folder,
  hardlinks it into the matching library folder, reads the link count (must be 2) and removes
  both. It also fails if the folders are on different filesystems, and says what to fix.
  This is the only check that writes anything; it leaves nothing behind.
- **Run it with `sudo`, or as a user in the media group.** The folders belong to `PUID:PGID` with
  mode 2775, so a login user outside that group cannot write the test file. Without write
  access the check still verifies that the folders exist and share a filesystem, then reports
  `SKIP ... hardlinks were NOT tested` with this hint instead of failing. If the user may run
  `sudo -n` without a password, the test runs automatically as `PUID:PGID` through it.
  Join the group with `sudo usermod -aG media <user>` (log in again afterwards) if you want the
  check, and manual work in `/data`, without `sudo`.
- Folder names are compared byte for byte: if the folder on disk is spelled differently
  (for example another Unicode form of `ö`), the script would create a second folder. Check
  with `ls /data` first.
- **The app's database folder `config/app`** (in this repository, not under `/data`) is made by
  the same script, owned by `PUID:PGID`, because the app runs as that user and cannot create it.
  If it already exists as `root:root` (Docker's doing), the script hands it over, that folder
  and `app.db*` only; it refuses symbolic links and hard-linked files there. This is the one existing folder it changes. Details: [app.md](app.md#database-issue-9).

## 5. docker-compose.yml skeleton and .env with VPN credentials

Create `.env` on the VM (never on your PC, never in chat, never on a command line):

```bash
cd /opt/youtube-premium
cp .env.example .env     # copy the template
chmod 600 .env           # only your user may read it, it will hold VPN credentials
nano .env                # edit: Ctrl+O saves, Ctrl+X quits
docker compose config --quiet   # silent = fine; otherwise it names the missing value
```

What to fill in (the template explains each line):

- `PUID`, `PGID`, `UMASK`, `DATA_ROOT`: from step 4.
- `LAN_SUBNET`: your LAN, already `192.168.178.0/24`. Gluetun lets this network
  through its firewall so the Web UI answers; everything else must use the tunnel.
- `VPN_SERVICE_PROVIDER=custom`, `OPENVPN_CUSTOM_CONFIG=...` and `VPN_TYPE=openvpn`: leave
  as they are. ExpressVPN works with OpenVPN only (not WireGuard, not Lightway), and
  gluetun's built-in `expressvpn` provider has been broken since 2026-11 (v3.41.3:
  "TLS key negotiation failed" on every server, its embedded key is stale). So gluetun
  uses ExpressVPN's own `.ovpn` file instead; see "The ExpressVPN config file" below.
- `OPENVPN_USER` / `OPENVPN_PASSWORD`: **not** your ExpressVPN e-mail and password.
  Log in to the ExpressVPN website, open *Set up on more devices* > *Manual
  configuration* > *OpenVPN*, and copy the user name and password shown there.
- `VPN_SERVER_COUNTRIES` / `VPN_SERVER_CITIES`: only for the built-in provider; leave
  them empty with `custom` (the server is the one named in your `.ovpn` file).
- The `WIREGUARD_*` lines are only read with `VPN_TYPE=wireguard`; with OpenVPN they
  stay as `changeme` and nothing complains. The reverse holds too: with `wireguard`
  the `OPENVPN_*` lines are ignored. The values of the **chosen** type are required, so
  `docker compose` refuses to start with a clear message if one is missing.

`.env` is gitignored; CI scans history for leaked secrets (`lint:secrets`).
The `watchdog` (a later issue) is behind the `planned` profile, so `docker compose up -d` only
starts what exists. The `proxy` needs `PROXY_USER` and `PROXY_PASSWORD_HASH` first: run
`make proxy-password` (and set `PROXY_BIND` to the VM's LAN address), see [proxy.md](proxy.md).

**ExpressVPN has no port forwarding.** Torrents still download, but nobody can
connect *to* you through the VPN, so you get fewer peers and slower speeds on
poorly seeded torrents. There is nothing to configure for it; do not add
`VPN_PORT_FORWARDING`.

### How the VPN container works (issue #2)

`gluetun` gets `NET_ADMIN` and `/dev/net/tun`, builds the tunnel, and acts as a
firewall: traffic that is not inside the tunnel is dropped, except to
`LAN_SUBNET`. It has a Docker health check of its own that is healthy only while
the tunnel works. `restart: unless-stopped` plus Docker starting on boot brings
the tunnel up after a host reboot. The image is pinned (`GLUETUN_TAG`, currently
`v3.41.3`); updates are deliberate (#30).

### The ExpressVPN config file

1. On the ExpressVPN website open *Set up on more devices > Manual configuration >
   OpenVPN*, pick the location you want and download the `.ovpn` file.
2. Put it on the VM as `config/expressvpn.ovpn` and protect it. It contains
   ExpressVPN's keys: never commit it, never paste or print it.

```bash
mkdir -p config && nano config/expressvpn.ovpn   # paste the file's content, Ctrl+O, Ctrl+X
chmod 600 config/expressvpn.ovpn
```

3. Render the file for gluetun and start the stack:

```bash
make up    # = scripts/vpn/render_ovpn.py, then docker compose up -d
```

Why a rendering step: gluetun's custom provider refuses a server *name* in the `remote`
line ("host is not an IP address"), and ExpressVPN's file has a name plus
`remote-random`. `scripts/vpn/render_ovpn.py` resolves the name to an IPv4 address, drops
`remote-random` (one fixed server) and writes `config/gluetun/custom/expressvpn.ovpn`
(mode 600), which the container reads as `/gluetun/custom/expressvpn.ovpn`. Everything
else in the file is copied unchanged, and the script never prints it. Server addresses
rotate: **if the tunnel stops connecting, run `make up` again** to look the name up
afresh. It is safe to re-run (`scripts/vpn/render_ovpn.py --dry-run` only reports).
(Alternatives considered: a sidecar container that renders on every `up`, which adds an
image and root-owned files, and a hand-edited IP, which goes stale.)

`config/gluetun/` is also gluetun's persistent `/gluetun` folder (server list and so
on). If Docker created it as root before you ran the script, fix it once with
`sudo chown -R "$USER" config/gluetun`.

```bash
docker compose ps                    # STATUS shows "healthy" after about a minute
docker compose logs gluetun | tail   # "Public IP address is ..." is the VPN address
```

## 5b. qBittorrent inside the tunnel, with a real login (issue #3)

`qbittorrent` uses `network_mode: service:gluetun`: it has no network of its own,
so it can only reach the internet through the tunnel, and **all** ports are
published on `gluetun` (the Web UI on `QBIT_WEBUI_BIND:QBIT_WEBUI_PORT`, localhost
only: the LAN reaches it through the reverse proxy, [docs/proxy.md](proxy.md)). It starts only
when gluetun is healthy.

The linuxserver image would start with a temporary random password. Replace that
with your own **before the first start**; the script stores only a hash:

```bash
docker compose stop qbittorrent 2>/dev/null || true   # qBittorrent rewrites its config on exit
scripts/qbittorrent/seed_config.py --user admin       # asks for the password twice (10+ characters)
make up                                               # or: docker compose up -d
```

It writes `config/qbittorrent/qBittorrent/qBittorrent.conf` (mode 600, gitignored) with
the user, the PBKDF2 hash, `tun0` as the network interface (an extra guard: with
no tunnel there is no `tun0`, so nothing connects), and the login required from
every client. Re-run it any time to change the password. `--dry-run` shows what it
would set.

**Verify:**

- `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/v2/app/version` prints `403`
  (the API refuses anyone who is not logged in).
- From your PC, open an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 <user>@<vm>`), browse to
  `http://127.0.0.1:8080` and log in with the user and password you just chose.
- `docker compose logs qbittorrent | grep -i "temporary password"` finds nothing.

(Never put the password on a command line: it ends up in the shell history.)

**The app logs in too.** Put the same user and password into `.env` as `QBIT_USER` /
`QBIT_PASSWORD` (the password is plain text there: `.env` is gitignored, keep it mode 600, and
only the `app` service receives it). Without it the app starts but says downloads are disabled.
qBittorrent itself still stores only the hash.

## 5c. Prove the killswitch (issue #4)

Run this once after the stack is up, and after any change to the VPN or network setup. It
stops gluetun on purpose, so downloads pause for a minute or two; gluetun is always
started again, even if the test fails.

```bash
make killswitch-test        # = scripts/vpn/killswitch-test.sh   (--dry-run shows the steps)
```

It does exactly this, and prints only the verdicts `VPN IP`, `no connection` and
`HOME IP LEAK` (never an address, so the output can be shared):

1. `docker exec qbittorrent curl ...` asks a "what is my IP" service. The answer must differ
   from the home address (the host's own): **VPN IP**.
2. `docker stop gluetun`, then the same request again must give **no connection**. If it
   answers with the home address, that is a **HOME IP LEAK** and the setup is wrong.
3. `docker start gluetun`: qbittorrent must have a connection again, with a VPN address,
   **automatically** (netwatch restarts the container; nobody has to).

Step 3 is the recovery path. qBittorrent shares gluetun's network namespace; when gluetun
restarts as the same container it gets a new namespace and qBittorrent keeps the dead
one (no network at all) until it is restarted. A small watcher inside the qBittorrent
container (`scripts/qbittorrent/services/netwatch`, an s6 service of the linuxserver image) notices
that the container has no network interface left for about 30 seconds and stops the
container; `restart: unless-stopped` starts it again inside the new namespace. It needs no
Docker socket. A tunnel that is merely down does not trigger it: gluetun's own interface is
still there and its firewall blocks everything. If recovery ever fails, restart by hand:
`docker restart qbittorrent`. After a `git pull` that changes the watcher, run `docker restart
qbittorrent` too: the linuxserver image copies `/custom-services.d` into s6 when the container
starts, so a running container keeps using the version it started with (the folder mount only
avoids the stale single-file inode, it does not reload the service). The qBittorrent container also reports `unhealthy` in
`docker compose ps` while the namespace is dead. The planned watchdog (#21) is the second
layer on top of this.

## 5d. qBittorrent categories (issue #6)

Four categories decide where a finished download is kept: `movies`, `series`, `audiobooks`
and `music`, each with the save path `/data/downloads/<category>` (as the container sees it).
The default save path is `/data/downloads`, unfinished torrents go to
`/data/downloads/incomplete`, and Automatic Torrent Management is on for new torrents (that
is what makes a torrent with a category land in the category's folder). It is code, not
clicking: the script talks to the running qBittorrent's Web API, so qBittorrent itself stores
and validates everything.

```bash
docker compose ps                              # qbittorrent must be running and healthy
scripts/qbittorrent/setup_categories.py --dry-run   # shows the plan, contacts nothing
scripts/qbittorrent/setup_categories.py        # asks for the Web UI password (admin)
```

It only creates or corrects those four categories and the settings above. Other categories
and settings are left alone, and re-running it changes nothing once everything is set. The
password is asked for on the terminal (or `--password-stdin`); it is never on a command line,
in a file or in the output. If the Web UI port is not 8080 on localhost, pass `--url`. Use the port qBittorrent itself listens
on: it answers `401` (shown as "login refused") also when the port in the address differs from
its own Web UI port, for example behind a `18080->8080` mapping.

**Verify with a real torrent (manual, once):** use a legal test torrent such as a Debian
installer ISO from debian.org (torrent link under "Download"). In the Web UI (SSH tunnel as in
step 5b): *Add Torrent File/Link*, set *Category* to `movies`, leave *Torrent Management Mode*
on **Automatic**, add it. While it downloads the file is in `/data/downloads/incomplete/`;
when it finishes it moves to `/data/downloads/movies/`. Check on the VM:

```bash
ls -la /data/downloads/movies/    # the ISO is here, owned by PUID:PGID
ls /data/Filme/                   # unchanged: nothing from the download appears in the library
```

Then remove the torrent **with its data** in the Web UI. Repeat with category `series` if you
like; the folders `audiobooks` and `music` work the same.

## 6. restart: unless-stopped on all services

Set once in the `x-service-defaults` anchor and merged into every service.
Enforced by `tests/unit/test_compose_skeleton.py` and, on the rendered config,
by `tests/integration/test_compose_config.py`.

## 7. Initialize Git and push to GitLab

```bash
git remote add origin git@<gitlab-host>:<group>/youtube-premium.git
git push -u origin main
git push -u origin 1-c0-provision-host   # then open the MR against main
```

## Acceptance

On the VM, with `.env` filled in:

```bash
make host-check     # all six checks PASS (virt, docker, compose, tun, media, layout)
make smoke-host     # smoke tests incl. the full host check
```
