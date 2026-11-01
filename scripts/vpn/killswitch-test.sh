#!/usr/bin/env bash
# killswitch-test.sh: prove qBittorrent cannot reach the internet outside the VPN (issue #4).
#
# Run on the Docker host with the stack up (`docker compose up -d`). It stops gluetun
# on purpose, so downloads pause for a minute or two; it always starts gluetun again.
#
# Steps:
#   1. qbittorrent reaches the internet, and with an address that is NOT the home one
#   2. docker stop gluetun: qbittorrent must have NO connectivity (no leak)
#   3. docker start gluetun: qbittorrent must get connectivity back BY ITSELF, with an
#      address that is again not the home one (see scripts/qbittorrent/netwatch.sh)
#
# Output: only the verdicts "VPN IP", "no connection" and "HOME IP LEAK". No address is
# ever printed, so the log can be shared.
#
# Usage:
#   scripts/vpn/killswitch-test.sh [--dry-run]
#
# Exit: 0 all steps passed, 1 a step failed, 2 the stack is not ready for the test.
#
# Environment (all optional):
#   KS_COMPOSE           compose command, default "docker compose" (e.g. with -p / --env-file)
#   KS_IP_URL            plain-text "what is my IP" URL, default https://api.ipify.org
#   KS_POLL_SECONDS      pause between polls, default 5
#   KS_HEALTHY_TIMEOUT   seconds to wait for gluetun to be healthy again, default 180
#   KS_RECOVERY_TIMEOUT  seconds to wait for qbittorrent to recover, default 300

set -uo pipefail

read -r -a COMPOSE <<<"${KS_COMPOSE:-docker compose}"
IP_URL="${KS_IP_URL:-https://api.ipify.org}"
POLL="${KS_POLL_SECONDS:-5}"
HEALTHY_TIMEOUT="${KS_HEALTHY_TIMEOUT:-180}"
RECOVERY_TIMEOUT="${KS_RECOVERY_TIMEOUT:-300}"
DRY_RUN=0

HOME_IP=""
GLUETUN=""
QBIT=""
STOPPED=0
FAILED=0

log() { printf '%s\n' "$*"; }
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
}

# The public address as seen from inside the qbittorrent container; empty if it has none.
qbit_address() {
  local out
  out="$(docker exec "$QBIT" curl -s --max-time "${1:-8}" "$IP_URL" 2>/dev/null)" || out=""
  out="${out//[[:space:]]/}"
  if [[ "$out" =~ ^[0-9a-fA-F.:]+$ ]]; then printf '%s' "$out"; fi
}

# Verdict for an address: VPN IP | no connection | HOME IP LEAK (never the address itself).
verdict() {
  if [ -z "$1" ]; then
    echo "no connection"
  elif [ "$1" = "$HOME_IP" ]; then
    echo "HOME IP LEAK"
  else
    echo "VPN IP"
  fi
}

wait_for_healthy() {
  local deadline=$((SECONDS + HEALTHY_TIMEOUT)) status
  while [ "$SECONDS" -lt "$deadline" ]; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$GLUETUN" 2>/dev/null || true)"
    [ "$status" = "healthy" ] && return 0
    sleep "$POLL"
  done
  return 1
}

dry_run() {
  log "dry run: the test would run these steps, changing nothing"
  log "+ ${COMPOSE[*]} ps -q gluetun qbittorrent"
  log "+ curl -s $IP_URL                              (host: the home address, kept private)"
  log "+ docker exec <qbittorrent> curl -s $IP_URL     (expect: VPN IP)"
  log "+ docker stop <gluetun>"
  log "+ docker exec <qbittorrent> curl -s $IP_URL     (expect: no connection)"
  log "+ docker start <gluetun>, wait until healthy"
  log "+ docker exec <qbittorrent> curl -s $IP_URL     (expect: VPN IP again, without restarting qbittorrent)"
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
  done
  if [ "$DRY_RUN" -eq 1 ]; then
    dry_run
    exit 0
  fi

  GLUETUN="$("${COMPOSE[@]}" ps -q gluetun 2>/dev/null | head -n 1)"
  QBIT="$("${COMPOSE[@]}" ps -q qbittorrent 2>/dev/null | head -n 1)"
  [ -n "$GLUETUN" ] && [ -n "$QBIT" ] || die "gluetun and qbittorrent are not running; start the stack first (docker compose up -d)"
  # Start gluetun again whatever happened, so the stack is never left without its VPN.
  trap '[ "$STOPPED" -eq 0 ] || { echo "restoring: starting gluetun again"; docker start "$GLUETUN" >/dev/null 2>&1; }' EXIT

  HOME_IP="$(curl -s --max-time 10 "$IP_URL" 2>/dev/null | tr -d '[:space:]')"
  [ -n "$HOME_IP" ] || die "cannot determine the home address from this host (no internet?)"

  log "Step 1: qbittorrent through the tunnel"
  local result
  result="$(verdict "$(qbit_address)")"
  if [ "$result" = "VPN IP" ]; then pass "$result"; else fail "$result (expected VPN IP)"; fi

  log "Step 2: gluetun stopped, qbittorrent must have no connectivity"
  docker stop "$GLUETUN" >/dev/null 2>&1 || die "could not stop gluetun"
  STOPPED=1
  sleep "$POLL"
  result="$(verdict "$(qbit_address 5)")"
  if [ "$result" = "no connection" ]; then
    pass "$result"
  else
    fail "$result (expected no connection)"
  fi

  log "Step 3: gluetun started again, qbittorrent must recover by itself"
  docker start "$GLUETUN" >/dev/null 2>&1 || die "could not start gluetun"
  STOPPED=0
  if wait_for_healthy; then
    pass "gluetun is healthy again"
  else
    fail "gluetun did not become healthy within ${HEALTHY_TIMEOUT}s"
  fi
  local start=$SECONDS deadline=$((SECONDS + RECOVERY_TIMEOUT))
  result="no connection"
  while [ "$SECONDS" -lt "$deadline" ]; do
    result="$(verdict "$(qbit_address)")"
    [ "$result" = "no connection" ] || break
    sleep "$POLL"
  done
  case "$result" in
    "VPN IP") pass "VPN IP again after $((SECONDS - start))s, without restarting qbittorrent" ;;
    "HOME IP LEAK") fail "HOME IP LEAK after gluetun came back" ;;
    *) fail "no connection ${RECOVERY_TIMEOUT}s after gluetun came back (manual fix: docker restart qbittorrent)" ;;
  esac

  if [ "$FAILED" -eq 0 ]; then
    log "OK: the killswitch holds and qbittorrent recovers"
    exit 0
  fi
  log "FAILED: see the FAIL lines above"
  exit 1
}

main "$@"
