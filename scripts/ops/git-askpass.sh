#!/usr/bin/env bash
# git-askpass.sh: lets git read the read-only GitLab token from a file (issue #42).
#
# git calls this (GIT_ASKPASS) with a prompt and prints the answer. The token is therefore never
# on a command line or in the URL, only in a file with mode 600:
#   $AUTODEPLOY_TOKEN_FILE   (set by scripts/ops/autodeploy.py)
set -euo pipefail

case "${1:-}" in
  Username*) printf '%s\n' "oauth2" ;;
  Password*) head -n 1 "${AUTODEPLOY_TOKEN_FILE:?AUTODEPLOY_TOKEN_FILE is not set}" ;;
  *) exit 1 ;;
esac
