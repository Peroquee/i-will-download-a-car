#!/usr/bin/env bash
# create-layout.sh: create the download and library folders under DATA_ROOT (issue #5, C2).
#
# Creates only what is MISSING, owned by PUID:PGID with mode 2775 (setgid, group-writable):
#   DATA_ROOT/downloads/{movies,series,audiobooks,music,incomplete}
#   DATA_ROOT/<MEDIA_DIR_MOVIES|SERIES|AUDIOBOOKS|MUSIC|YOUTUBE>   (the library folders)
# Folders that already exist are left exactly as they are: no chmod, no chown, no
# recursion, their contents are never touched. Safe to re-run.
#
# Why two trees: unfinished downloads must never show up in Jellyfin, whose libraries
# point at the library folders. Finished files are HARDLINKED from downloads/ into them,
# which needs both on one filesystem (scripts/host/check-host.sh --only layout proves it).
#
# Needs root (use sudo) unless you already are PUID:PGID: creating a folder owned by
# another user is a privileged operation. The login user need not be in the media group.
#
# Usage:
#   sudo scripts/host/create-layout.sh [--env-file PATH] [--dry-run]
#
#   --env-file  settings to read (default .env): DATA_ROOT, PUID, PGID, MEDIA_DIR_*
#   --dry-run   print what would be created, change nothing
#
# Test hook: CHECK_ROOT prefixes every filesystem path (default empty).

set -euo pipefail

CHECK_ROOT="${CHECK_ROOT:-}"
ENV_FILE=".env"
DRY_RUN=0

readonly DOWNLOAD_DIRS="movies series audiobooks music incomplete"
readonly MEDIA_VARS="MEDIA_DIR_MOVIES MEDIA_DIR_SERIES MEDIA_DIR_AUDIOBOOKS MEDIA_DIR_MUSIC MEDIA_DIR_YOUTUBE"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}

# Read KEY from the env file without sourcing it (it holds secrets and arbitrary values).
env_value() {
  local key="$1" line
  [ -f "$ENV_FILE" ] || return 1
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1)" || return 1
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# A folder name relative to DATA_ROOT: one path component, nothing that could climb out.
valid_dirname() {
  case "$1" in
    ""|"."|".."|*/*|-*) return 1 ;;
  esac
  [[ "$1" != *[[:cntrl:]]* ]]
}

# Create DIR if it is missing; leave an existing folder alone.
ensure_dir() {
  local dir="$1"
  if [ -d "$dir" ]; then
    log "exists   $dir"
  elif [ -e "$dir" ]; then
    die "$dir exists but is not a folder"
  else
    log "creating $dir"
    run install -d -m 2775 -o "$PUID" -g "$PGID" "$dir"
  fi
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --env-file)
        [ $# -ge 2 ] || die "--env-file needs a value"
        ENV_FILE="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  [ -f "$ENV_FILE" ] || die "env file $ENV_FILE not found (copy .env.example to .env)"

  local data_root PUID PGID
  data_root="$(env_value DATA_ROOT || true)"
  PUID="$(env_value PUID || true)"
  PGID="$(env_value PGID || true)"
  [ -n "$data_root" ] || die "DATA_ROOT is missing in $ENV_FILE"
  [[ "$data_root" == /* ]] || die "DATA_ROOT must be an absolute path"
  [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]] || die "PUID and PGID must be numeric in $ENV_FILE"
  { [ "$PUID" != 0 ] && [ "$PGID" != 0 ]; } || die "PUID/PGID must not be root (0)"

  local var name dir
  local -a library_dirs=()
  for var in $MEDIA_VARS; do
    name="$(env_value "$var" || true)"
    [ -n "$name" ] || die "$var is missing in $ENV_FILE (see .env.example)"
    valid_dirname "$name" || die "$var='$name' must be a single folder name relative to DATA_ROOT"
    for dir in "${library_dirs[@]:-}"; do
      [ "$dir" != "$name" ] || die "$var='$name' is used twice; every library needs its own folder"
    done
    library_dirs+=("$name")
  done

  local root="${CHECK_ROOT}${data_root}"
  [ -d "$root" ] || die "$root does not exist: mount the media volume first (scripts/host/bootstrap.sh)"
  if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ] \
    && { [ "$(id -u)" != "$PUID" ] || [ "$(id -g)" != "$PGID" ]; }; then
    die "creating folders owned by $PUID:$PGID needs root: run it with sudo"
  fi

  log "Download folders (unfinished and finished torrents; Jellyfin does not see them)"
  ensure_dir "$root/downloads"
  for dir in $DOWNLOAD_DIRS; do
    ensure_dir "$root/downloads/$dir"
  done

  log "Library folders (what Jellyfin's libraries point at)"
  for name in "${library_dirs[@]}"; do
    ensure_dir "$root/$name"
  done
  log "Done. Prove hardlinks work: scripts/host/check-host.sh --only layout"
}

main "$@"
