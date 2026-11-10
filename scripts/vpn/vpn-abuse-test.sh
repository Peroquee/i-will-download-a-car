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
#   tunnel   cut the way to the VPN server inside gluetun's network namespace (everything that
#            leaves on its outer interface and is not a private address is dropped): OpenVPN and
#            gluetun's control server keep running, and the control server still says "running",
#            while the tunnel is dead. This is the incident of 2026-11-10 (#81): "running" is not
#            "connected". The watchdog must still go down, name the cause "tunnel" and pause. It
#            takes WATCHDOG_TUNNEL_GRACE (default 180 s) plus a few seconds: a dead tunnel has to
#            last that long before it counts as an outage (gluetun reconnects by itself).
#   tunnel-restart  as "tunnel", but keeps the tunnel cut until the WATCHDOG restarts the VPN
#            (WATCHDOG_RESTART_AFTER seconds, default 900: for this test set it to 120 in .env and
#            recreate the watchdog, then put it back). Not in the default rotation.
# and checks, per cycle:
#   1. the watchdog notices (its state turns "down") within AB_DETECT_TIMEOUT seconds;
#   2. no leak: while the VPN is down a probe inside qbittorrent's network tries to reach the
#      internet every AB_PROBE_INTERVAL seconds; it must never show the HOME address, and must
#      see "no connection" at least once (so the window was real);
#   3. vpn mode: all torrents are paused within AB_DETECT_TIMEOUT seconds of the watchdog
#      noticing (in the other modes qBittorrent is unreachable together with gluetun, so a pause
#      cannot be sent, and there is no connection to leak through either);
#      tunnel modes: gluetun said "running" while the watchdog had not yet gone down (the incident's
#      state was reproduced), the watchdog's cause is "tunnel", its reason says "Tunnel not
#      connected", it logged the outage, and an alert was sent if ALERT_URL is set;
#   4. recovery: gluetun healthy, the watchdog "ok", qBittorrent has a VPN address again;
#   5. resume behaviour matches WATCHDOG_RESUME_AFTER: 0 = the torrents stay paused (the script
#      then resumes them itself), N = they are running again N seconds after the VPN is stable.
#
# Output: one PASS/FAIL line per check and a table at the end. NO address is ever printed.
#
# Usage:
#   scripts/vpn/vpn-abuse-test.sh [--cycles N] [--modes stop,kill,restart,vpn,tunnel,tunnel-restart]
#                                 [--allow-no-torrent] [--dry-run]
#   make vpn-abuse-test CYCLES=20
#   make tunnel-test            one cycle of the mode "tunnel" (the #81 scenario)
#   make tunnel-restart-test    one cycle of "tunnel-restart" (needs WATCHDOG_RESTART_AFTER=120)
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
#   AB_OUTER_IF          gluetun's outer network interface (tunnel modes), default eth0
#   AB_TUNNEL_DETECT_TIMEOUT   seconds for the watchdog to notice a dead tunnel, default: its
#                        WATCHDOG_TUNNEL_GRACE (read from the container) + 90
#   AB_TUNNEL_RECOVERY_TIMEOUT seconds for gluetun to reconnect after the cut is removed (OpenVPN
#                        retries every few minutes), default 600
#   AB_RESTART_EXTRA     tunnel-restart: seconds to wait for the restart beyond
#                        WATCHDOG_RESTART_AFTER + WATCHDOG_TUNNEL_GRACE, default 120

set -uo pipefail

read -r -a COMPOSE <<<"${AB_COMPOSE:-docker compose}"
PROBE_URL="${AB_PROBE_URL:-https://1.1.1.1/cdn-cgi/trace}"
PROBE_INTERVAL="${AB_PROBE_INTERVAL:-0.5}"
POLL="${AB_POLL_SECONDS:-1}"
DETECT_TIMEOUT="${AB_DETECT_TIMEOUT:-20}"
HEALTHY_TIMEOUT="${AB_HEALTHY_TIMEOUT:-240}"
RECOVERY_TIMEOUT="${AB_RECOVERY_TIMEOUT:-300}"
OUTER_IF="${AB_OUTER_IF:-eth0}"
TUNNEL_DETECT_TIMEOUT="${AB_TUNNEL_DETECT_TIMEOUT:-}"
TUNNEL_RECOVERY_TIMEOUT="${AB_TUNNEL_RECOVERY_TIMEOUT:-600}"
RESTART_EXTRA="${AB_RESTART_EXTRA:-120}"
CYCLES=5
MODES="stop kill restart vpn tunnel"
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
CUT=0
SEEN_RUNNING=0
TUNNEL_GRACE=180
RESTART_AFTER=0
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

# Cut the way to the VPN server inside gluetun's network namespace: whatever leaves on the outer
# interface to a non-private address is dropped, private addresses (the LAN, the docker network,
# so the control server, the watchdog and qBittorrent) still work. OpenVPN keeps running.
cut_tunnel() {
  CUT=1
  docker exec "$GLUETUN" sh -c "
    iptables -I OUTPUT 1 -o '$OUTER_IF' -j DROP &&
    iptables -I OUTPUT 1 -o '$OUTER_IF' -d 10.0.0.0/8 -j ACCEPT &&
    iptables -I OUTPUT 1 -o '$OUTER_IF' -d 172.16.0.0/12 -j ACCEPT &&
    iptables -I OUTPUT 1 -o '$OUTER_IF' -d 192.168.0.0/16 -j ACCEPT" >/dev/null 2>&1
}

# Remove the cut again (also the half of it, if the cut failed in the middle); safe to repeat.
heal_tunnel() {
  [ "$CUT" -eq 1 ] || return 0
  docker exec "$GLUETUN" sh -c "
    for rule in '-j DROP' '-d 10.0.0.0/8 -j ACCEPT' '-d 172.16.0.0/12 -j ACCEPT' '-d 192.168.0.0/16 -j ACCEPT'; do
      while iptables -D OUTPUT -o '$OUTER_IF' \$rule 2>/dev/null; do :; done
    done" >/dev/null 2>&1
  CUT=0
}

apply_mode() {
  case "$1" in
    stop) STOPPED=1; docker stop -t 2 "$GLUETUN" >/dev/null 2>&1 ;;
    kill) STOPPED=1; docker kill "$GLUETUN" >/dev/null 2>&1 ;;
    restart) docker restart -t 2 "$GLUETUN" >/dev/null 2>&1 ;;
    vpn) docker exec "$GLUETUN" sh -c 'pkill -9 openvpn || kill -9 "$(pidof openvpn)"' >/dev/null 2>&1 ;;
    tunnel|tunnel-restart) cut_tunnel ;;
    *) return 1 ;;
  esac
}

restore() {
  heal_tunnel
  if [ "$STOPPED" -eq 1 ]; then
    docker start "$GLUETUN" >/dev/null 2>&1
    STOPPED=0
  fi
}

# ---------------------------------------------------------------------------- one cycle

is_tunnel_mode() { [ "$1" = "tunnel" ] || [ "$1" = "tunnel-restart" ]; }

# Like wait_for down, but while it waits it notes whether gluetun SAYS "running": in the incident
# it did, for hours, and a test that never saw that did not reproduce it. Sets SEEN_RUNNING.
wait_for_down_while_running() {
  local timeout="$1" start=$SECONDS
  SEEN_RUNNING=0
  while :; do
    [ "$(wd_probe gluetun)" = "running" ] && [ "$(wd_state)" != "down" ] && SEEN_RUNNING=1
    if condition down; then ELAPSED=$((SECONDS - start)); return 0; fi
    [ $((SECONDS - start)) -lt "$timeout" ] || { ELAPSED=$((SECONDS - start)); return 1; }
    sleep "$POLL"
  done
}

# The checks that only make sense when the process runs and the tunnel does not (#81).
tunnel_checks() {
  local since="$1" cause reason
  if [ "$SEEN_RUNNING" -eq 1 ]; then
    pass "gluetun said 'running' while the tunnel was dead (the state of the incident)"
  else
    fail "gluetun never said 'running' before the watchdog went down: this did not reproduce the incident"
  fi
  cause="$(wd_probe cause)"
  if [ "$cause" = "tunnel" ]; then
    pass "the watchdog names the cause: tunnel"
  else
    fail "the watchdog's cause is '${cause:-?}', not 'tunnel'"
  fi
  reason="$(wd_probe reason)"
  case "$reason" in
    "Tunnel not connected"*) pass "the dashboard reason reads: $reason" ;;
    *) fail "the watchdog's reason does not say 'Tunnel not connected': '${reason:-?}'" ;;
  esac
  if docker logs --since "$since" "$WATCHDOG" 2>&1 | grep -q 'VPN unhealthy'; then
    pass "the watchdog logged the outage"
  else
    fail "the watchdog did not log 'VPN unhealthy'"
  fi
  if docker exec "$WATCHDOG" sh -c 'test -n "$ALERT_URL"' >/dev/null 2>&1; then
    if docker logs --since "$since" "$WATCHDOG" 2>&1 | grep -q 'alert sent'; then
      pass "an alert was sent (ALERT_URL is set)"
    else
      fail "no alert was sent although ALERT_URL is set"
    fi
  else
    log "NOTE  ALERT_URL is not set: no alert to check"
  fi
}

# Mode tunnel-restart: the tunnel stays cut until the watchdog restarts the VPN.
wait_for_restart() {
  local timeout=$((RESTART_AFTER + TUNNEL_GRACE + RESTART_EXTRA)) start=$SECONDS restarts
  while :; do
    restarts="$(wd_probe restarts)"
    if [ "${restarts:-0}" -ge 1 ] 2>/dev/null; then ELAPSED=$((SECONDS - start)); return 0; fi
    [ $((SECONDS - start)) -lt "$timeout" ] || { ELAPSED=$((SECONDS - start)); return 1; }
    sleep "$POLL"
  done
}

run_cycle() {
  local n="$1" mode="$2" detect="-" pausedcol="n/a" recovered="-" resumecol="-"
  local detect_timeout="$DETECT_TIMEOUT" recovery_timeout="$RECOVERY_TIMEOUT" healthy_timeout="$HEALTHY_TIMEOUT"
  local since
  if is_tunnel_mode "$mode"; then
    detect_timeout="$TUNNEL_DETECT_TIMEOUT"
    recovery_timeout="$TUNNEL_RECOVERY_TIMEOUT"
    healthy_timeout="$TUNNEL_RECOVERY_TIMEOUT"
  fi
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
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if ! apply_mode "$mode"; then
    leak_probe_stop
    fail "could not apply the failure mode '$mode'"
    ROWS+=("$n|$mode|-|-|-|-|-|FAIL")
    restore
    return
  fi

  # 1. the watchdog notices
  local noticed=1
  if is_tunnel_mode "$mode"; then
    wait_for_down_while_running "$detect_timeout" || noticed=0
  else
    wait_for down "$detect_timeout" || noticed=0
  fi
  if [ "$noticed" -eq 1 ]; then
    detect="$((SECONDS - t0))s"
    pass "watchdog noticed after $detect"
  else
    fail "the watchdog did not turn 'down' within ${detect_timeout}s"
  fi
  if is_tunnel_mode "$mode"; then tunnel_checks "$since"; fi

  # 3. vpn and tunnel modes: qBittorrent stays reachable, the pause can be delivered, so it must be
  if [ "$mode" = "vpn" ] || is_tunnel_mode "$mode"; then
    if wait_for paused "$DETECT_TIMEOUT"; then
      pausedcol="yes"
      pass "all torrents paused within ${ELAPSED}s of the watchdog noticing"
    else
      pausedcol="NO"
      fail "torrents were still running ${DETECT_TIMEOUT}s after the watchdog noticed (running=$(running_count))"
    fi
  fi

  # tunnel-restart: the cut stays until the watchdog has restarted the VPN by itself
  if [ "$mode" = "tunnel-restart" ]; then
    if wait_for_restart; then
      pass "the watchdog restarted the VPN after ${ELAPSED}s more (WATCHDOG_RESTART_AFTER=${RESTART_AFTER})"
    else
      fail "the watchdog did not restart the VPN within $((RESTART_AFTER + TUNNEL_GRACE + RESTART_EXTRA))s"
    fi
  fi

  # restore the VPN and wait for everything to come back
  restore
  if wait_for healthy "$healthy_timeout"; then
    pass "gluetun healthy again"
  else
    fail "gluetun did not become healthy within ${healthy_timeout}s"
  fi
  if wait_for ok "$recovery_timeout"; then
    recovered="${ELAPSED}s"
  else
    fail "the watchdog did not return to 'ok' within ${recovery_timeout}s"
  fi
  if wait_for vpnip "$recovery_timeout"; then
    pass "qbittorrent has a VPN address again (recovery ${ELAPSED}s after the watchdog was ok)"
  else
    fail "qbittorrent has no VPN address ${recovery_timeout}s after gluetun came back (manual fix: docker restart qbittorrent)"
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
  if [ "$mode" = "vpn" ] || is_tunnel_mode "$mode"; then
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

# Only when a tunnel mode is selected: can the cut be made, does the watchdog know the new probes,
# and how long may it take (its WATCHDOG_TUNNEL_GRACE).
tunnel_preflight() {
  local mode wanted=0 grace restart_after
  for mode in $MODES; do is_tunnel_mode "$mode" && wanted=1; done
  [ "$wanted" -eq 1 ] || return 0
  docker exec "$GLUETUN" iptables -S OUTPUT >/dev/null 2>&1 \
    || die "cannot run iptables inside gluetun (the tunnel modes need it)"
  docker exec "$GLUETUN" ip link show "$OUTER_IF" >/dev/null 2>&1 \
    || die "gluetun has no interface '$OUTER_IF' (set AB_OUTER_IF)"
  [ "$(wd_probe gluetun)" = "running" ] \
    || die "the watchdog cannot ask gluetun (is the watchdog image up to date? docker compose up -d --build watchdog)"
  [ "$(wd_probe address)" = "present" ] || die "gluetun has no public address now: fix the VPN first"
  grace="$(docker exec "$WATCHDOG" printenv WATCHDOG_TUNNEL_GRACE 2>/dev/null || echo 180)"
  grace="${grace%%.*}"
  [[ "$grace" =~ ^[0-9]+$ ]] && TUNNEL_GRACE="$grace"
  [ -n "$TUNNEL_DETECT_TIMEOUT" ] || TUNNEL_DETECT_TIMEOUT=$((TUNNEL_GRACE + 90))
  restart_after="$(docker exec "$WATCHDOG" printenv WATCHDOG_RESTART_AFTER 2>/dev/null || echo 0)"
  restart_after="${restart_after%%.*}"
  [[ "$restart_after" =~ ^[0-9]+$ ]] && RESTART_AFTER="$restart_after"
  for mode in $MODES; do
    if [ "$mode" = "tunnel-restart" ]; then
      [ "$RESTART_AFTER" -gt 0 ] || die "mode tunnel-restart needs WATCHDOG_RESTART_AFTER > 0 in .env"
      [ "$RESTART_AFTER" -le 900 ] || log "NOTE  WATCHDOG_RESTART_AFTER is ${RESTART_AFTER}s: the tunnel-restart cycle takes that long"
    fi
  done
  log "tunnel modes: the watchdog needs ${TUNNEL_GRACE}s of dead tunnel before it counts as an outage (detect timeout ${TUNNEL_DETECT_TIMEOUT}s)"
}

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
      tunnel) log "  cycle $i: drop non-private traffic on gluetun's $OUTER_IF (OpenVPN and the control server keep running); watchdog must go down, cause 'tunnel'" ;;
      tunnel-restart) log "  cycle $i: as 'tunnel', and keep it cut until the watchdog restarts the VPN" ;;
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
    case "$mode" in stop|kill|restart|vpn|tunnel|tunnel-restart) ;; *) die "unknown mode '$mode' (stop, kill, restart, vpn, tunnel, tunnel-restart)" ;; esac
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
  trap 'leak_probe_stop; [ -z "$PROBE_FILE" ] || rm -f "$PROBE_FILE"; heal_tunnel; [ "$STOPPED" -eq 0 ] || { echo "restoring: starting gluetun again"; restore; }; resume_initial' EXIT

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
  tunnel_preflight

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
