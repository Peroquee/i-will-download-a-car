#!/usr/bin/env bash
# install.sh: install or maintain youtube-premium; asks questions in plain language (issue #37).
#
# For a fresh Debian 12 computer or VM. Start it from the project folder:
#   ./install.sh                  new installation, or a menu if one exists (change/update/repair)
#   ./install.sh --update         back up, fetch the new version, rebuild, check
#   ./install.sh --repair         run every step again and check
#   ./install.sh --status         what is installed and running
#   ./install.sh --dry-run        show what would be done, change nothing (no sudo needed)
#   ./install.sh --lang de|en     skip the language question
#   ./install.sh --answers FILE   no questions: read a private (chmod 600) answers file
#
# This script only checks the system, becomes root (sudo) and starts scripts/install/install.py,
# where the conversation lives. Safe to run again; stop with Ctrl+C at any time.
# Test hooks: OS_RELEASE (default /etc/os-release).
# Hilfe / Help: HILFE-MIT-KI.md, AI-HELP.md, docs/installer.md

set -euo pipefail

readonly REPO_URL="${YTP_REPO_URL:-https://github.com/Peroquee/i-will-download-a-car.git}"
OS_RELEASE="${OS_RELEASE:-/etc/os-release}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf 'FEHLER / ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in -h|--help) usage; exit 0 ;; esac

DRY_RUN=0
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRY_RUN=1
done

# 1. Debian 12 only.
if [ -r "$OS_RELEASE" ]; then
  # shellcheck source=/dev/null
  os_id="$(. "$OS_RELEASE" && printf '%s' "${ID:-}")"
  # shellcheck source=/dev/null
  os_codename="$(. "$OS_RELEASE" && printf '%s' "${VERSION_CODENAME:-}")"
else
  os_id=""
  os_codename=""
fi
if [ "$os_id" != "debian" ] || [ "$os_codename" != "bookworm" ]; then
  die "Nur Debian 12 (bookworm) wird unterstützt, hier: ${os_id:-?} ${os_codename:-?}. / Only Debian 12 (bookworm) is supported, found: ${os_id:-?} ${os_codename:-?}. (docs/host-setup.md)"
fi

# 2. The project files must be here. A lone install.sh downloads the project first.
if [ ! -f "$HERE/scripts/install/install.py" ]; then
  command -v git >/dev/null 2>&1 || die "git fehlt / git is missing: sudo apt-get install -y git"
  [ ! -e youtube-premium ] || die "Der Ordner youtube-premium existiert schon. / The folder youtube-premium already exists. cd youtube-premium && ./install.sh"
  git clone "$REPO_URL" youtube-premium
  exec "$PWD/youtube-premium/install.sh" "$@"
fi

# 3. Root (the installer installs packages, creates users and folders).
if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    printf 'Die Installation braucht Administratorrechte; sudo fragt nach deinem Passwort.\n'
    printf 'The installation needs administrator rights; sudo will ask for your password.\n'
    exec sudo -E bash "$HERE/install.sh" "$@"
  fi
  die "sudo fehlt. Werde zuerst root (\"su -\") und starte ./install.sh dort. / sudo is missing. Become root first (\"su -\") and run ./install.sh there."
fi

# 4. Python 3 runs the conversation (Debian 12 usually has it).
if ! command -v python3 >/dev/null 2>&1; then
  [ "$DRY_RUN" -eq 0 ] || die "python3 fehlt / python3 is missing: apt-get install -y python3"
  apt-get update
  apt-get install -y python3
fi

cd "$HERE"
exec python3 "$HERE/scripts/install/install.py" "$@"
