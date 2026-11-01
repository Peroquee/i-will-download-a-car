#!/usr/bin/env bash
# check-host.sh: verify the Docker host meets the base requirements (issue #1, C0).
#
# Read-only, with one exception: the `layout` check creates and removes a small hidden
# temp file in each download and library folder to prove hardlinks work. Run it on the
# Debian VM after bootstrap.sh, and after any host change. CI runs a subset as a smoke test.
#
# Usage:
#   scripts/host/check-host.sh [--only virt,docker,compose,tun,media,layout] [--env-file PATH]
#
# Output: one line per check, "PASS|FAIL|SKIP <check> <detail>", then a summary.
# Exit:   0 all selected checks passed (SKIP counts as pass), 1 any FAIL, 2 bad usage.
#
# Test hooks (used by tests/unit, never needed in normal use):
#   CHECK_ROOT  prefix for filesystem paths, so tests can point at a fake tree.

set -uo pipefail

readonly ALL_CHECKS="virt docker compose tun media layout"
readonly MIN_COMPOSE="2.17"   # depends_on.restart needs 2.17+
readonly TUN_DEVNUM="a:c8"    # /dev/net/tun is char device 10:200 (hex a:c8)

CHECK_ROOT="${CHECK_ROOT:-}"
ENV_FILE=".env"
SELECTED="$ALL_CHECKS"
FAILURES=0

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

pass() { printf 'PASS %-8s %s\n' "$1" "$2"; }
skip() { printf 'SKIP %-8s %s\n' "$1" "$2"; }
fail() { printf 'FAIL %-8s %s\n' "$1" "$2"; FAILURES=$((FAILURES + 1)); }

# Read KEY from the env file without sourcing it (the file holds secrets and
# arbitrary values; sourcing would execute it).
env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 1
  local line
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1)" || return 1
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# version_ge A B: true if dotted version A >= B (major.minor compared numerically).
version_ge() {
  local a_major a_minor b_major b_minor
  IFS=. read -r a_major a_minor _ <<<"${1#v}"
  IFS=. read -r b_major b_minor _ <<<"${2#v}"
  a_minor="${a_minor:-0}"; b_minor="${b_minor:-0}"
  [[ "$a_major" =~ ^[0-9]+$ && "$a_minor" =~ ^[0-9]+$ ]] || return 1
  (( a_major > b_major || (a_major == b_major && a_minor >= b_minor) ))
}

check_virt() {
  if ! command -v systemd-detect-virt >/dev/null 2>&1; then
    skip virt "systemd-detect-virt not available"
    return
  fi
  local container vm
  container="$(systemd-detect-virt --container 2>/dev/null || true)"
  if [ -n "$container" ] && [ "$container" != "none" ]; then
    fail virt "running inside a container ($container); Docker must run in a full VM so the gluetun killswitch works"
    return
  fi
  vm="$(systemd-detect-virt --vm 2>/dev/null || true)"
  if [ -z "$vm" ] || [ "$vm" = "none" ]; then
    pass virt "bare metal (expected a Proxmox VM, but this works too)"
  else
    pass virt "virtual machine ($vm)"
  fi
}

check_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    fail docker "docker CLI not installed"
    return
  fi
  local version
  if ! version="$(docker info --format '{{.ServerVersion}}' 2>/dev/null)" || [ -z "$version" ]; then
    fail docker "docker daemon not reachable (is it running, and is this user in the docker group?)"
    return
  fi
  pass docker "Docker Engine $version"
}

check_compose() {
  if ! command -v docker >/dev/null 2>&1; then
    fail compose "docker CLI not installed"
    return
  fi
  local version
  if ! version="$(docker compose version --short 2>/dev/null)" || [ -z "$version" ]; then
    fail compose "Compose plugin not installed (docker compose)"
    return
  fi
  if version_ge "$version" "$MIN_COMPOSE"; then
    pass compose "Compose $version"
  else
    fail compose "Compose $version is too old, need >= $MIN_COMPOSE"
  fi
}

check_tun() {
  local tun="${CHECK_ROOT}/dev/net/tun"
  if [ ! -e "$tun" ]; then
    fail tun "$tun missing; load the tun module (modprobe tun) or give the VM a kernel with it"
    return
  fi
  if [ ! -c "$tun" ]; then
    fail tun "$tun exists but is not a character device"
    return
  fi
  local devnum
  devnum="$(stat -L -c '%t:%T' "$tun" 2>/dev/null || true)"
  if [ "$devnum" != "$TUN_DEVNUM" ]; then
    fail tun "$tun has device number $devnum, expected 10:200"
    return
  fi
  pass tun "$tun is the TUN device (10:200)"
}

check_media() {
  if [ ! -f "$ENV_FILE" ]; then
    fail media "env file $ENV_FILE not found (copy .env.example to .env)"
    return
  fi
  local data_root puid pgid umask_value
  data_root="$(env_value DATA_ROOT || true)"
  puid="$(env_value PUID || true)"
  pgid="$(env_value PGID || true)"
  umask_value="$(env_value UMASK || true)"

  local missing=""
  [ -n "$data_root" ] || missing="$missing DATA_ROOT"
  [ -n "$puid" ] || missing="$missing PUID"
  [ -n "$pgid" ] || missing="$missing PGID"
  [ -n "$umask_value" ] || missing="$missing UMASK"
  if [ -n "$missing" ]; then
    fail media "missing in $ENV_FILE:$missing"
    return
  fi

  if ! [[ "$puid" =~ ^[0-9]+$ && "$pgid" =~ ^[0-9]+$ ]]; then
    fail media "PUID and PGID must be numeric (got $puid:$pgid)"
    return
  fi
  if [ "$puid" = "0" ] || [ "$pgid" = "0" ]; then
    fail media "PUID/PGID must not be root (0)"
    return
  fi
  if ! [[ "$umask_value" =~ ^0?[0-7]{3}$ ]]; then
    fail media "UMASK must be an octal mask like 002 (got $umask_value)"
    return
  fi

  local path="${CHECK_ROOT}${data_root}"
  if [ ! -d "$path" ]; then
    fail media "$path does not exist"
    return
  fi
  if ! mountpoint -q "$path"; then
    fail media "$path is not a mount point; the media volume is not attached"
    return
  fi
  local owner mode
  owner="$(stat -c '%u:%g' "$path")"
  if [ "$owner" != "$puid:$pgid" ]; then
    fail media "$path is owned by $owner, expected PUID:PGID $puid:$pgid"
    return
  fi
  mode="$(stat -c '%a' "$path")"
  # Group must be able to write, otherwise UMASK 002 sharing cannot work.
  if (( (8#$mode & 8#020) == 0 )); then
    fail media "$path mode $mode is not group-writable"
    return
  fi
  pass media "$path mounted, owner $owner, mode $mode, umask $umask_value"
}

# Download and library folders exist, share one filesystem, and a hardlink between them
# works (link count 2). Fails with a hint if folders are missing (create-layout.sh).
check_layout() {
  if [ ! -f "$ENV_FILE" ]; then
    fail layout "env file $ENV_FILE not found (copy .env.example to .env)"
    return
  fi
  local data_root var name missing=""
  data_root="$(env_value DATA_ROOT || true)"
  if [ -z "$data_root" ]; then
    fail layout "DATA_ROOT missing in $ENV_FILE"
    return
  fi
  local -a types=(movies series audiobooks music) names=()
  for var in MEDIA_DIR_MOVIES MEDIA_DIR_SERIES MEDIA_DIR_AUDIOBOOKS MEDIA_DIR_MUSIC MEDIA_DIR_YOUTUBE; do
    name="$(env_value "$var" || true)"
    if [ -n "$name" ]; then names+=("$name"); else missing="$missing $var"; fi
  done
  if [ -n "$missing" ]; then
    fail layout "missing in $ENV_FILE:$missing (see .env.example)"
    return
  fi

  local root="${CHECK_ROOT}${data_root}" absent="" i
  [ -d "$root/downloads" ] || absent="$absent downloads"
  for i in "${!types[@]}"; do
    [ -d "$root/downloads/${types[$i]}" ] || absent="$absent downloads/${types[$i]}"
  done
  for name in "${names[@]}"; do
    [ -d "$root/$name" ] || absent="$absent $name"
  done
  if [ -n "$absent" ]; then
    fail layout "missing folders under $root:$absent (run: sudo scripts/host/create-layout.sh)"
    return
  fi

  local device
  device="$(stat -c '%d' "$root/downloads")"
  for name in "${names[@]}"; do
    if [ "$(stat -c '%d' "$root/$name")" != "$device" ]; then
      fail layout "$root/$name is on a different filesystem than $root/downloads: hardlinks cannot work"
      return
    fi
  done

  local src dst links err
  for i in "${!types[@]}"; do
    src="$root/downloads/${types[$i]}/.ytp-linktest.$$"
    dst="$root/${names[$i]}/.ytp-linktest.$$"
    if ! : >"$src" 2>/dev/null; then
      fail layout "cannot write to $root/downloads/${types[$i]} (owner and mode: sudo scripts/host/create-layout.sh)"
      return
    fi
    if ! err="$(ln "$src" "$dst" 2>&1)"; then
      rm -f "$src" "$dst"
      fail layout "hardlink downloads/${types[$i]} -> ${names[$i]} failed: $err"
      return
    fi
    links="$(stat -c '%h' "$src")"
    rm -f "$src" "$dst"
    if [ "$links" != "2" ]; then
      fail layout "hardlink downloads/${types[$i]} -> ${names[$i]} has link count $links, expected 2"
      return
    fi
  done
  pass layout "hardlinks work from downloads/ into ${names[0]}, ${names[1]}, ${names[2]}, ${names[3]} (link count 2); ${names[4]} is on the same filesystem"
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --only)
        [ $# -ge 2 ] || { echo "--only needs a value" >&2; exit 2; }
        SELECTED="${2//,/ }"; shift 2 ;;
      --env-file)
        [ $# -ge 2 ] || { echo "--env-file needs a value" >&2; exit 2; }
        ENV_FILE="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
  done

  local check
  for check in $SELECTED; do
    case " $ALL_CHECKS " in
      *" $check "*) ;;
      *) echo "unknown check: $check (valid: $ALL_CHECKS)" >&2; exit 2 ;;
    esac
  done

  # Explicit dispatch (not "check_$check"): shellcheck cannot see calls through
  # a computed name and would flag every check_* helper as unused (SC2329).
  for check in $SELECTED; do
    case "$check" in
      virt) check_virt ;;
      docker) check_docker ;;
      compose) check_compose ;;
      tun) check_tun ;;
      media) check_media ;;
      layout) check_layout ;;
      *) echo "no dispatch for check: $check" >&2; exit 2 ;;
    esac
  done

  if [ "$FAILURES" -eq 0 ]; then
    echo "OK: all checks passed"
    exit 0
  fi
  echo "FAILED: $FAILURES check(s) failed"
  exit 1
}

main "$@"
