#!/usr/bin/env bash
# create-layout.sh: create the download and library folders under DATA_ROOT (issue #5, C2).
#
# Creates only what is MISSING, owned by PUID:PGID with mode 2775 (setgid, group-writable):
#   DATA_ROOT/downloads/{movies,series,audiobooks,music,incomplete}
#   DATA_ROOT/<MEDIA_DIR_MOVIES|SERIES|AUDIOBOOKS|MUSIC|YOUTUBE>   (the library folders)
# Folders that already exist are left exactly as they are: no chmod, no chown, no
# recursion, their contents are never touched. Safe to re-run.
#
# One exception, the app's own state folder config/app (the SQLite database, #9) in this
# repository: it is created owned by PUID:PGID too, and if it exists with another owner (Docker
# makes a missing bind-mount source as root:root) it is handed to PUID:PGID, non-recursively
# (the folder and app.db*). The app runs as PUID:PGID and cannot do that itself.
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
# Test hooks: CHECK_ROOT prefixes every filesystem path under DATA_ROOT (default empty);
# APP_CONFIG_DIR replaces the repository's config/app.

set -euo pipefail

CHECK_ROOT="${CHECK_ROOT:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_CONFIG_DIR="${APP_CONFIG_DIR:-$REPO_ROOT/config/app}"
ENV_FILE=".env"
DRY_RUN=0

readonly DOWNLOAD_DIRS="movies series audiobooks music incomplete"
readonly MEDIA_VARS="MEDIA_DIR_MOVIES MEDIA_DIR_SERIES MEDIA_DIR_AUDIOBOOKS MEDIA_DIR_MUSIC MEDIA_DIR_YOUTUBE"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
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

# The app's state folder: owned by PUID:PGID, created if missing, handed over if it is not
# (see the header). The parent (config/) stays owned by whoever owns the repository, so
# the login user can still put files like config/expressvpn.ovpn there.
ensure_app_config() {
  local dir="$1" parent owner file
  parent="$(dirname "$dir")"
  if [ ! -d "$parent" ]; then
    log "creating $parent"
    run install -d -m 755 -o "$(stat -c '%u' "$REPO_ROOT")" -g "$(stat -c '%g' "$REPO_ROOT")" "$parent"
  fi
  if [ ! -e "$dir" ]; then
    log "creating $dir"
    run install -d -m 2775 -o "$PUID" -g "$PGID" "$dir"
  elif [ ! -d "$dir" ]; then
    die "$dir exists but is not a folder"
  else
    owner="$(stat -c '%u:%g' "$dir")"
    if [ "$owner" = "$PUID:$PGID" ]; then
      log "exists   $dir"
    else
      log "fixing   $dir (owned by $owner; the app runs as $PUID:$PGID and must write there)"
      run chown "$PUID:$PGID" "$dir"
      run chmod 2775 "$dir"
      for file in app.db app.db-wal app.db-shm; do
        if [ -e "$dir/$file" ]; then
          run chown "$PUID:$PGID" "$dir/$file"
        fi
      done
    fi
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
  log "App state folder (the app's database; the app runs as $PUID:$PGID)"
  ensure_app_config "$APP_CONFIG_DIR"

  log "Done. Prove hardlinks work: scripts/host/check-host.sh --only layout"
}

main "$@"
