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

## 5. docker-compose.yml skeleton and .env with VPN credentials

```bash
cp .env.example .env && chmod 600 .env
$EDITOR .env      # VPN provider, WireGuard key and address, LAN subnet, PUID/PGID
docker compose config --quiet   # fails with a clear message if anything required is missing
```

`.env` is gitignored; CI scans history for leaked secrets (`lint:secrets`).
Services for later issues (`app`, `watchdog`, `proxy`) are behind the
`planned` profile, so `docker compose up -d` only starts what exists.

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
make host-check     # all five checks PASS
make smoke-host     # smoke tests incl. the full host check
```
