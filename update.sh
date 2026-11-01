#!/usr/bin/env bash
# update.sh: update youtube-premium: back up, fetch the new version, rebuild, check (issue #37).
#
# The same as `./install.sh --update`. Any other option of install.sh works here too (--dry-run,
# --lang de|en). Safe to run again.

set -euo pipefail

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --update "$@"
