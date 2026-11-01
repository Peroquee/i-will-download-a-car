#!/usr/bin/env bash
# vpn-abuse-test.sh: break the VPN over and over and prove nothing leaks (issue #24).
#
# Run on the Docker host with the whole stack up and healthy (including the watchdog, see
# docs/watchdog.md) and at least one torrent RUNNING in qBittorrent (add a small legal test
# torrent). It REALLY drops the VPN: downloads stop for a minute or so per cycle. It refuses to
# start unless the stack is healthy, always starts gluetun again, and resumes the torrents it
# paused.
#
# Every cycle uses one failure mode (rotating through the selected modes):
#   stop     docker stop gluetun            the whole container goes away
#   kill     docker kill gluetun            killed with SIGKILL, no clean shutdown
#   restart  docker restart gluetun         down and up again by itself
#   vpn      kill -9 openvpn inside gluetun the container stays up, only the tunnel dies, so
#                                           qBittorrent stays reachable and the pause CAN be sent
# and checks, per cycle:
#   1. the watchdog notices (its state turns "down") within AB_DETECT_TIMEOUT seconds;
#   2. no leak: while the VPN is down a probe inside qbittorrent's network tries to reach the
#      internet every AB_PROBE_INTERVAL seconds; it must never show the HOME address, and must
#      see "no connection" at least once (so the window was real);
#   3. vpn mode: all torrents are paused within AB_DETECT_TIMEOUT seconds of the watchdog
#      noticing (in the other modes qBittorrent is unreachable together with gluetun, so a pause
#      cannot be sent, and there is no connection to leak through either);
#   4. recovery: gluetun healthy, the watchdog "ok", qBittorrent has a VPN address again;
#   5. resume behaviour matches WATCHDOG_RESUME_AFTER: 0 = the torrents stay paused (the script
#      then resumes them itself), N = they are running again N seconds after the VPN is stable.
#
# Output: one PASS/FAIL line per check and a table at the end. NO address is ever printed.
#
# Usage:
#   scripts/vpn/vpn-abuse-test.sh [--cycles N] [--modes stop,kill,restart,vpn]
#                                 [--allow-no-torrent] [--dry-run]
#   make vpn-abuse-test CYCLES=20
#
# Exit: 0 every check of every cycle passed, 1 a check failed, 2 the stack is not ready.
#
# Environment (all optional):
#   AB_COMPOSE           compose command, default "docker compose"
#   AB_PROBE_URL         what the probe asks, default https://1.1.1.1/cdn-cgi/trace (an address,
#                        no DNS needed; the reply's "ip=" line, or a plain-text body, is read)
#   AB_PROBE_INTERVAL    seconds between leak probes, default 0.5
#   AB_POLL_SECONDS      pause between polls, default 1
#   AB_DETECT_TIMEOUT    seconds for the watchdog to notice / to pause, default 20
#   AB_HEALTHY_TIMEOUT   seconds for gluetun to be healthy again, default 240
#   AB_RECOVERY_TIMEOUT  seconds for watchdog and qBittorrent to recover, default 300

set -uo pipefail

read -r -a COMPOSE <<<"${AB_COMPOSE:-docker compose}"
PROBE_URL="${AB_PROBE_URL:-https://1.1.1.1/cdn-cgi/trace}"
PROBE_INTERVAL="${AB_PROBE_INTERVAL:-0.5}"
POLL="${AB_POLL_SECONDS:-1}"
DETECT_TIMEOUT="${AB_DETECT_TIMEOUT:-20}"
HEALTHY_TIMEOUT="${AB_HEALTHY_TIMEOUT:-240}"
RECOVERY_TIMEOUT="${AB_RECOVERY_TIMEOUT:-300}"
CYCLES=5
MODES="stop kill restart vpn"
REQUIRE_TORRENT=1
DRY_RUN=0

GLUETUN=""
QBIT=""
WATCHDOG=""
HOME_IP=""
RESUME_AFTER=0
INITIAL_RUNNING=0
INITIAL_HASHES=""
STOPPED=0
PROBE_PID=""
PROBE_FILE=""
FAILED=0
ELAPSED=0
declare -a ROWS=()

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; CYCLE_OK=0; }

usage() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------- probes

# Ask the watchdog container (python -m ytp.watchdog --probe ...): plain words, no secrets.
wd_probe() { docker exec "$WATCHDOG" python -m ytp.watchdog --probe "$@" 2>/dev/null; }

wd_state() {
  local state
  state="$(wd_probe state)" || state=""
  printf '%s' "${state:-unreachable}"
}

# Running torrent count as "N" ("?" if qBittorrent cannot be asked).
running_count() {
  local counts
  counts="$(wd_probe counts)" || { printf '?'; return; }
  counts="${counts#running=}"
  printf '%s' "${counts%% *}"
}

# The public address as seen from inside qbittorrent; empty if it has none. Never printed.
qbit_address() {
  local out
  out="$(docker exec "$QBIT" curl -s --max-time "${1:-3}" "$PROBE_URL" 2>/dev/null)" || out=""
  if [[ "$out" == *ip=* ]]; then
    out="$(printf '%s\n' "$out" | sed -n 's/^ip=//p' | head -n 1)"
  fi
  out="${out//[[:space:]]/}"
  if [[ "$out" =~ ^[0-9a-fA-F.:]+$ ]]; then printf '%s' "$out"; fi
}

# VPN IP | no connection | HOME IP LEAK  (never the address)
verdict() {
  if [ -z "$1" ]; then
    echo "no connection"
  elif [ "$1" = "$HOME_IP" ]; then
    echo "HOME IP LEAK"
  else
    echo "VPN IP"
  fi
}

# Background leak probe: one line per attempt in $PROBE_FILE: leak | none | vpn.
leak_probe_start() {
  PROBE_FILE="$(mktemp)"
  (
    while :; do
      case "$(verdict "$(qbit_address 2)")" in
        "HOME IP LEAK") echo leak ;;
        "no connection") echo none ;;
        *) echo vpn ;;
      esac >>"$PROBE_FILE"
      sleep "$PROBE_INTERVAL"
    done
  ) &
  PROBE_PID=$!
}

leak_probe_stop() {
  if [ -n "$PROBE_PID" ]; then
    kill "$PROBE_PID" 2>/dev/null
    wait "$PROBE_PID" 2>/dev/null
    PROBE_PID=""
  fi
}

probe_count() { grep -c "^$1\$" "$PROBE_FILE" 2>/dev/null || true; }

# ---------------------------------------------------------------------------- waiting

# One named condition. (Explicit dispatch: shellcheck cannot see calls through a variable name.)
condition() {
  case "$1" in
    down) [ "$(wd_state)" = "down" ] ;;
    ok) [ "$(wd_state)" = "ok" ] ;;
    healthy) [ "$(docker inspect -f '{{.State.Health.Status}}' "$GLUETUN" 2>/dev/null)" = "healthy" ] ;;
    paused) [ "$(running_count)" = "0" ] ;;
    running) [ "$(running_count)" = "$INITIAL_RUNNING" ] ;;
    vpnip) [ "$(verdict "$(qbit_address 5)")" = "VPN IP" ] ;;
    *) return 1 ;;
  esac
}

# wait_for CONDITION TIMEOUT: poll until the condition holds; sets ELAPSED (seconds waited).
wait_for() {
  local name="$1" timeout="$2" start=$SECONDS
  while :; do
    if condition "$name"; then ELAPSED=$((SECONDS - start)); return 0; fi
    [ $((SECONDS - start)) -lt "$timeout" ] || { ELAPSED=$((SECONDS - start)); return 1; }
    sleep "$POLL"
  done
}

# ---------------------------------------------------------------------------- failure modes

apply_mode() {
  case "$1" in
    stop) STOPPED=1; docker stop -t 2 "$GLUETUN" >/dev/null 2>&1 ;;
    kill) STOPPED=1; docker kill "$GLUETUN" >/dev/null 2>&1 ;;
    restart) docker restart -t 2 "$GLUETUN" >/dev/null 2>&1 ;;
    vpn) docker exec "$GLUETUN" sh -c 'pkill -9 openvpn || kill -9 "$(pidof openvpn)"' >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

restore() {
  if [ "$STOPPED" -eq 1 ]; then
    docker start "$GLUETUN" >/dev/null 2>&1
    STOPPED=0
  fi
}

# ---------------------------------------------------------------------------- one cycle

run_cycle() {
  local n="$1" mode="$2" detect="-" pausedcol="n/a" recovered="-" resumecol="-"
  CYCLE_OK=1
  log ""
  log "Cycle $n/$CYCLES: mode $mode"

  # Starting point of every cycle.
  wait_for healthy "$HEALTHY_TIMEOUT" || fail "gluetun is not healthy at the start of the cycle"
  wait_for ok "$RECOVERY_TIMEOUT" || fail "the watchdog is not 'ok' at the start of the cycle"
  wait_for vpnip "$RECOVERY_TIMEOUT" || fail "qbittorrent has no VPN address at the start of the cycle"
  if [ "$CYCLE_OK" -eq 0 ]; then
    ROWS+=("$n|$mode|-|-|-|-|-|FAIL")
    return
  fi

  leak_probe_start
  local t0=$SECONDS
  if ! apply_mode "$mode"; then
    leak_probe_stop
    fail "could not apply the failure mode '$mode'"
    ROWS+=("$n|$mode|-|-|-|-|-|FAIL")
    restore
    return
  fi

  # 1. the watchdog notices
  if wait_for down "$DETECT_TIMEOUT"; then
    detect="$((SECONDS - t0))s"
    pass "watchdog noticed after $detect"
  else
    fail "the watchdog did not turn 'down' within ${DETECT_TIMEOUT}s"
  fi

  # 3. vpn mode: the pause can be delivered, so it must be
  if [ "$mode" = "vpn" ]; then
    if wait_for paused "$DETECT_TIMEOUT"; then
      pausedcol="yes"
      pass "all torrents paused within ${ELAPSED}s of the watchdog noticing"
    else
      pausedcol="NO"
      fail "torrents were still running ${DETECT_TIMEOUT}s after the watchdog noticed (running=$(running_count))"
    fi
  fi

  # restore the VPN and wait for everything to come back
  restore
  if wait_for healthy "$HEALTHY_TIMEOUT"; then
    pass "gluetun healthy again"
  else
    fail "gluetun did not become healthy within ${HEALTHY_TIMEOUT}s"
  fi
  if wait_for ok "$RECOVERY_TIMEOUT"; then
    recovered="${ELAPSED}s"
  else
    fail "the watchdog did not return to 'ok' within ${RECOVERY_TIMEOUT}s"
  fi
  if wait_for vpnip "$RECOVERY_TIMEOUT"; then
    pass "qbittorrent has a VPN address again (recovery ${ELAPSED}s after the watchdog was ok)"
  else
    fail "qbittorrent has no VPN address ${RECOVERY_TIMEOUT}s after gluetun came back (manual fix: docker restart qbittorrent)"
  fi

  # 2. no leak during the whole window
  leak_probe_stop
  local leaks nones probes
  leaks="$(probe_count leak)"; nones="$(probe_count none)"; probes="$(wc -l <"$PROBE_FILE")"
  rm -f "$PROBE_FILE"
  if [ "${leaks:-0}" -gt 0 ]; then
    fail "HOME IP LEAK: seen $leaks time(s) in $probes probes during the cycle"
  elif [ "${nones:-0}" -lt 1 ]; then
    fail "the probe never saw 'no connection' in $probes probes: the failure window was not observed"
  else
    pass "no leak in $probes probes ($nones during the outage)"
  fi

  # 5. resume behaviour
  if [ "$mode" = "vpn" ]; then
    if [ "$RESUME_AFTER" -gt 0 ]; then
      if wait_for running $((RESUME_AFTER + DETECT_TIMEOUT + 15)); then
        resumecol="resumed"
        pass "torrents resumed by the watchdog (WATCHDOG_RESUME_AFTER=${RESUME_AFTER})"
      else
        resumecol="NOT"
        fail "torrents were not resumed within $((RESUME_AFTER + DETECT_TIMEOUT + 15))s"
      fi
    else
      if [ "$(running_count)" = "0" ]; then
        resumecol="stay paused"
        pass "torrents stay paused (WATCHDOG_RESUME_AFTER=0); resuming them now"
      else
        resumecol="NOT paused"
        fail "torrents are running again although auto-resume is off"
      fi
      resume_initial
    fi
  else
    resumecol="untouched"
  fi
  if ! wait_for running "$RECOVERY_TIMEOUT"; then
    fail "the torrents that were running at the start are not all running again"
  fi

  local result="PASS"
  [ "$CYCLE_OK" -eq 1 ] || result="FAIL"
  ROWS+=("$n|$mode|$detect|$pausedcol|${leaks:-0}|$recovered|$resumecol|$result")
}

resume_initial() {
  [ -n "$INITIAL_HASHES" ] && wd_probe resume "$INITIAL_HASHES" >/dev/null
}

# ---------------------------------------------------------------------------- the rest

table() {
  log ""
  printf '%-6s %-8s %-8s %-8s %-6s %-10s %-12s %s\n' cycle mode detect paused leaks recovery resume result
  local row
  for row in "${ROWS[@]}"; do
    IFS='|' read -r c m d p l r s res <<<"$row"
    printf '%-6s %-8s %-8s %-8s %-6s %-10s %-12s %s\n' "$c" "$m" "$d" "$p" "$l" "$r" "$s" "$res"
  done
}

dry_run() {
  log "dry run: the test would do this, changing nothing"
  log "  preflight: gluetun healthy, watchdog 'ok', qbittorrent has a VPN address, >=1 running torrent"
  local i mode
  local -a modes
  read -r -a modes <<<"$MODES"
  for ((i = 1; i <= CYCLES; i++)); do
    mode="${modes[$(((i - 1) % ${#modes[@]}))]}"
    case "$mode" in
      stop) log "  cycle $i: docker stop gluetun" ;;
      kill) log "  cycle $i: docker kill gluetun" ;;
      restart) log "  cycle $i: docker restart gluetun" ;;
      vpn) log "  cycle $i: kill -9 openvpn inside gluetun" ;;
    esac
    log "    + leak probe in qbittorrent every ${PROBE_INTERVAL}s, watchdog 'down' within ${DETECT_TIMEOUT}s"
    log "    + recovery: gluetun healthy, watchdog 'ok', VPN address, resume behaviour"
  done
  log "  then a summary table; exit 1 if any check failed"
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --cycles)
        [ $# -ge 2 ] || { echo "--cycles needs a number" >&2; exit 2; }
        CYCLES="$2"; shift 2 ;;
      --modes)
        [ $# -ge 2 ] || { echo "--modes needs a list" >&2; exit 2; }
        MODES="${2//,/ }"; shift 2 ;;
      --allow-no-torrent) REQUIRE_TORRENT=0; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
  done
  [[ "$CYCLES" =~ ^[1-9][0-9]*$ ]] || die "--cycles must be a positive number"
  local mode
  for mode in $MODES; do
    case "$mode" in stop|kill|restart|vpn) ;; *) die "unknown mode '$mode' (stop, kill, restart, vpn)" ;; esac
  done
  [ -n "$MODES" ] || die "--modes is empty"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry_run
    exit 0
  fi

  GLUETUN="$("${COMPOSE[@]}" ps -q gluetun 2>/dev/null | head -n 1)"
  QBIT="$("${COMPOSE[@]}" ps -q qbittorrent 2>/dev/null | head -n 1)"
  WATCHDOG="$("${COMPOSE[@]}" ps -q watchdog 2>/dev/null | head -n 1)"
  [ -n "$GLUETUN" ] && [ -n "$QBIT" ] && [ -n "$WATCHDOG" ] \
    || die "gluetun, qbittorrent and watchdog must all be running (docker compose up -d)"
  # Whatever happens: stop the probe, start gluetun again, resume what the test paused.
  trap 'leak_probe_stop; [ -z "$PROBE_FILE" ] || rm -f "$PROBE_FILE"; [ "$STOPPED" -eq 0 ] || { echo "restoring: starting gluetun again"; restore; }; resume_initial' EXIT

  # Refuse unless everything is healthy right now.
  condition healthy || die "gluetun is not healthy; fix the stack before the abuse test"
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$WATCHDOG" 2>/dev/null)" = "healthy" ] \
    || die "the watchdog is not healthy (docker compose ps; did you run make watchdog-key?)"
  [ "$(wd_state)" = "ok" ] || die "the watchdog does not report 'ok'"
  HOME_IP="$(curl -s --max-time 10 "https://api.ipify.org" 2>/dev/null | tr -d '[:space:]')"
  [ -n "$HOME_IP" ] || die "cannot determine the home address from this host (no internet?)"
  [ "$(verdict "$(qbit_address 8)")" = "VPN IP" ] || die "qbittorrent is not on a VPN address now"
  INITIAL_RUNNING="$(running_count)"
  [[ "$INITIAL_RUNNING" =~ ^[0-9]+$ ]] || die "cannot read the torrents from qBittorrent"
  INITIAL_HASHES="$(wd_probe hashes)"
  if [ "$INITIAL_RUNNING" -lt 1 ] && [ "$REQUIRE_TORRENT" -eq 1 ]; then
    die "no torrent is running: add a small legal test torrent first (or --allow-no-torrent to skip the pause checks)"
  fi
  RESUME_AFTER="$(docker exec "$WATCHDOG" printenv WATCHDOG_RESUME_AFTER 2>/dev/null || echo 0)"
  RESUME_AFTER="${RESUME_AFTER%%.*}"
  [[ "$RESUME_AFTER" =~ ^[0-9]+$ ]] || RESUME_AFTER=0

  log "VPN abuse test: $CYCLES cycle(s), modes: $MODES, $INITIAL_RUNNING torrent(s) running, auto-resume after ${RESUME_AFTER}s (0 = off)"
  log "The VPN will be dropped on purpose; downloads stop for a minute per cycle."

  local i
  local -a modes
  read -r -a modes <<<"$MODES"
  for ((i = 1; i <= CYCLES; i++)); do
    run_cycle "$i" "${modes[$(((i - 1) % ${#modes[@]}))]}"
  done

  table
  if [ "$FAILED" -eq 0 ]; then
    log "OK: $CYCLES cycle(s), the watchdog noticed every time, nothing leaked, everything recovered"
    exit 0
  fi
  log "FAILED: see the FAIL lines and the table above"
  exit 1
}

main "$@"
