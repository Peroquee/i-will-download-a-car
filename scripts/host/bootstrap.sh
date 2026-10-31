#!/usr/bin/env bash
# bootstrap.sh: turn a fresh Debian 12 VM into the Docker host (issue #1, C0).
#
# Idempotent: safe to run again; steps already done are skipped.
#
# Usage (as root on the VM):
#   scripts/host/bootstrap.sh --user USER [--media-device /dev/sdb | --media-uuid UUID]
#                             [--format-ext4] [--data-root /data] [--puid 1000] [--pgid 1000]
#                             [--dry-run]
#
#   --user          login user to add to the docker group
#   --media-device  block device of the media volume; its UUID goes into fstab
#   --media-uuid    filesystem UUID of the media volume (alternative to --media-device)
#   --format-ext4   create ext4 on --media-device ONLY if it has no filesystem yet
#   --data-root     mount point of the media volume (default /data)
#   --puid/--pgid   owner of the media volume (default 1000/1000)
#   --dry-run       print every command instead of running it (no root needed)
#
# Test hooks: OS_RELEASE (default /etc/os-release), FSTAB_FILE (default /etc/fstab).

set -euo pipefail

OS_RELEASE="${OS_RELEASE:-/etc/os-release}"
FSTAB_FILE="${FSTAB_FILE:-/etc/fstab}"
DRY_RUN=0
LOGIN_USER=""
MEDIA_DEVICE=""
MEDIA_UUID=""
FORMAT_EXT4=0
DATA_ROOT="/data"
PUID=1000
PGID=1000

readonly DOCKER_PACKAGES="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
}

# Run a command, or print it in dry-run mode.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}

# Append LINE to FILE unless an identical line is already there.
append_line_once() {
  local file="$1" line="$2"
  if [ -f "$file" ] && grep -qxF -- "$line" "$file"; then
    log "already present in $file: $line"
    return
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ append to %s: %s\n' "$file" "$line"
  else
    printf '%s\n' "$line" >>"$file"
  fi
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --user) LOGIN_USER="${2:?--user needs a value}"; shift 2 ;;
      --media-device) MEDIA_DEVICE="${2:?--media-device needs a value}"; shift 2 ;;
      --media-uuid) MEDIA_UUID="${2:?--media-uuid needs a value}"; shift 2 ;;
      --format-ext4) FORMAT_EXT4=1; shift ;;
      --data-root) DATA_ROOT="${2:?--data-root needs a value}"; shift 2 ;;
      --puid) PUID="${2:?--puid needs a value}"; shift 2 ;;
      --pgid) PGID="${2:?--pgid needs a value}"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done

  [ -n "$LOGIN_USER" ] || die "--user is required"
  [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]] || die "--puid/--pgid must be numeric"
  [ "$PUID" != 0 ] && [ "$PGID" != 0 ] || die "--puid/--pgid must not be 0 (root)"
  [[ "$DATA_ROOT" == /* ]] || die "--data-root must be an absolute path"
  if [ -n "$MEDIA_DEVICE" ] && [ -n "$MEDIA_UUID" ]; then
    die "use either --media-device or --media-uuid, not both"
  fi
  if [ "$FORMAT_EXT4" -eq 1 ] && [ -z "$MEDIA_DEVICE" ]; then
    die "--format-ext4 needs --media-device"
  fi
}

check_preconditions() {
  if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    die "must run as root (or use --dry-run)"
  fi
  [ -r "$OS_RELEASE" ] || die "cannot read $OS_RELEASE"
  local id codename
  # shellcheck source=/dev/null
  id="$(. "$OS_RELEASE" && printf '%s' "${ID:-}")"
  # shellcheck source=/dev/null
  codename="$(. "$OS_RELEASE" && printf '%s' "${VERSION_CODENAME:-}")"
  [ "$id" = "debian" ] && [ "$codename" = "bookworm" ] \
    || die "expected Debian 12 (bookworm), found ${id:-unknown} ${codename:-unknown}"
}

install_docker() {
  log "Installing Docker Engine and the Compose plugin from download.docker.com"
  run apt-get update
  run apt-get install -y ca-certificates curl qemu-guest-agent
  run install -m 0755 -d /etc/apt/keyrings
  run curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  run chmod a+r /etc/apt/keyrings/docker.asc
  local arch
  arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
  local repo="deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ write /etc/apt/sources.list.d/docker.list: %s\n' "$repo"
  else
    printf '%s\n' "$repo" >/etc/apt/sources.list.d/docker.list
  fi
  run apt-get update
  # Word splitting of the package list is intended.
  # shellcheck disable=SC2086
  run apt-get install -y $DOCKER_PACKAGES
  run systemctl enable --now docker qemu-guest-agent
  run usermod -aG docker "$LOGIN_USER"
}

ensure_tun() {
  log "Making sure /dev/net/tun exists now and after reboots"
  if [ ! -c /dev/net/tun ]; then
    run modprobe tun
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ write /etc/modules-load.d/tun.conf: tun\n'
  else
    printf 'tun\n' >/etc/modules-load.d/tun.conf
  fi
}

ensure_media_owner() {
  log "Ensuring media owner ${PUID}:${PGID} exists"
  if ! getent group "$PGID" >/dev/null 2>&1; then
    run groupadd --gid "$PGID" media
  fi
  if ! getent passwd "$PUID" >/dev/null 2>&1; then
    run useradd --uid "$PUID" --gid "$PGID" --no-create-home --shell /usr/sbin/nologin media
  fi
}

attach_media_volume() {
  if [ -z "$MEDIA_DEVICE" ] && [ -z "$MEDIA_UUID" ]; then
    log "No media volume given; creating $DATA_ROOT on the system disk (attach a volume before real use)"
    run install -d -m 2775 -o "$PUID" -g "$PGID" "$DATA_ROOT"
    return
  fi

  if [ -n "$MEDIA_DEVICE" ]; then
    local fstype=""
    fstype="$(blkid -s TYPE -o value "$MEDIA_DEVICE" 2>/dev/null || true)"
    if [ -z "$fstype" ]; then
      [ "$FORMAT_EXT4" -eq 1 ] || die "$MEDIA_DEVICE has no filesystem; rerun with --format-ext4 to create one"
      log "Creating ext4 on empty device $MEDIA_DEVICE"
      run mkfs.ext4 -L media "$MEDIA_DEVICE"
    elif [ "$FORMAT_EXT4" -eq 1 ]; then
      log "$MEDIA_DEVICE already has a $fstype filesystem; NOT formatting"
    fi
    if [ "$DRY_RUN" -eq 1 ] && [ -z "$fstype" ]; then
      MEDIA_UUID="<uuid-after-mkfs>"
    else
      MEDIA_UUID="$(blkid -s UUID -o value "$MEDIA_DEVICE")"
    fi
    [ -n "$MEDIA_UUID" ] || die "could not read the UUID of $MEDIA_DEVICE"
  fi

  log "Mounting media volume UUID=$MEDIA_UUID at $DATA_ROOT"
  run install -d -m 0755 "$DATA_ROOT"
  # nofail: the VM still boots if the volume is missing; the stack must then
  # not start on an empty directory, which check-host.sh (mountpoint) catches.
  append_line_once "$FSTAB_FILE" "UUID=${MEDIA_UUID} ${DATA_ROOT} ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2"
  run systemctl daemon-reload
  run mount "$DATA_ROOT"
  run chown "$PUID:$PGID" "$DATA_ROOT"
  # setgid: new files and folders inherit the media group.
  run chmod 2775 "$DATA_ROOT"
}

main() {
  parse_args "$@"
  check_preconditions
  install_docker
  ensure_tun
  ensure_media_owner
  attach_media_volume
  log "Done. Log out and back in for docker group membership, then run scripts/host/check-host.sh"
}

main "$@"
