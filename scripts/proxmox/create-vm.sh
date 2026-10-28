#!/usr/bin/env bash
# create-vm.sh: create the Debian 12 VM for the stack on the Proxmox host (issue #1, C0).
#
# Run as root on the Proxmox node. Prints every qm command before running it.
#
# Usage:
#   scripts/proxmox/create-vm.sh --vmid 210 --iso local:iso/debian-12.X.0-amd64-netinst.iso \
#       [--name youtube-premium] [--storage local-lvm] [--bridge vmbr0] \
#       [--media-size 500] [--media-storage local-lvm] [--dry-run]
#
#   --media-size     size in GB of a second disk for /data; omit if media comes
#                    from elsewhere (NFS, passthrough), see docs/host-setup.md
#
# Sizing (from the issue): 4 vCPU, 4 GB RAM, 32 GB system disk.
# onboot=1 with startup order 1, so the VPN stack comes up first after a host reboot (#32).

set -euo pipefail

VMID=""; ISO=""; NAME="youtube-premium"; STORAGE="local-lvm"; BRIDGE="vmbr0"
MEDIA_SIZE=""; MEDIA_STORAGE=""; DRY_RUN=0
readonly CORES=4 MEMORY_MB=4096 SYSTEM_DISK_GB=32

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() { printf '+ %s\n' "$*"; [ "$DRY_RUN" -eq 1 ] || "$@"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --vmid) VMID="${2:?}"; shift 2 ;;
    --iso) ISO="${2:?}"; shift 2 ;;
    --name) NAME="${2:?}"; shift 2 ;;
    --storage) STORAGE="${2:?}"; shift 2 ;;
    --bridge) BRIDGE="${2:?}"; shift 2 ;;
    --media-size) MEDIA_SIZE="${2:?}"; shift 2 ;;
    --media-storage) MEDIA_STORAGE="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "$VMID" =~ ^[0-9]+$ ]] || die "--vmid must be numeric"
[ -n "$ISO" ] || die "--iso is required"
[ -z "$MEDIA_SIZE" ] || [[ "$MEDIA_SIZE" =~ ^[0-9]+$ ]] || die "--media-size must be a number of GB"
MEDIA_STORAGE="${MEDIA_STORAGE:-$STORAGE}"

if [ "$DRY_RUN" -eq 0 ]; then
  command -v qm >/dev/null || die "qm not found; run this on the Proxmox host"
  if qm status "$VMID" >/dev/null 2>&1; then
    die "VM $VMID already exists"
  fi
fi

run qm create "$VMID" \
  --name "$NAME" \
  --ostype l26 \
  --machine q35 \
  --bios ovmf \
  --efidisk0 "${STORAGE}:1,efitype=4m,pre-enrolled-keys=0" \
  --cpu host \
  --cores "$CORES" \
  --memory "$MEMORY_MB" \
  --balloon 0 \
  --scsihw virtio-scsi-single \
  --scsi0 "${STORAGE}:${SYSTEM_DISK_GB},iothread=1,discard=on,ssd=1" \
  --net0 "virtio,bridge=${BRIDGE}" \
  --ide2 "${ISO},media=cdrom" \
  --boot "order=scsi0;ide2" \
  --agent enabled=1 \
  --onboot 1 \
  --startup order=1,up=30

if [ -n "$MEDIA_SIZE" ]; then
  # backup=0: media is large and replaceable; back it up separately if wanted.
  run qm set "$VMID" --scsi1 "${MEDIA_STORAGE}:${MEDIA_SIZE},iothread=1,discard=on,backup=0"
fi

echo "VM $VMID created. Start it, install Debian 12 (no desktop, SSH server), then run scripts/host/bootstrap.sh inside it."
