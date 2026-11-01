"""scripts/vpn/vpn-abuse-test.sh against a stateful stub docker: verdicts, restore, no address."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.shell import ScriptResult, StubBin, run_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vpn" / "vpn-abuse-test.sh"
HOME_IP = "84.150.20.30"
VPN_IP = "185.107.12.34"
HASH = "a" * 40

# A fake docker that models gluetun, the watchdog and qBittorrent with a few state files.
#   phase:  up | down (container stopped) | recovering (container coming back) | vpn (tunnel dead)
#   STUB_SCENARIO: ok | leak | blind | no-pause | no-recovery | unhealthy | no-torrent | resume
DOCKER = f"""
st="$STUB_STATE"
phase() {{ cat "$st/phase" 2>/dev/null || echo up; }}
set_phase() {{ echo "$1" >"$st/phase"; echo 0 >"$st/tick"; }}
tick() {{ n=$(( $(cat "$st/tick" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$st/tick"; echo "$n"; }}
running() {{ cat "$st/running" 2>/dev/null || echo 1; }}
log() {{ echo "$*" >>"$st/log"; }}
case "$1" in
  compose) echo "id-${{@: -1}}" ;;
  stop) log "stop ${{@: -1}}"; set_phase down ;;
  kill) log "kill $2"; set_phase down ;;
  start) log "start $2"; set_phase recovering ;;
  restart) log "restart"; set_phase recovering ;;
  inspect)
    case "$4" in
      id-watchdog) echo healthy ;;
      *) [ "$STUB_SCENARIO" = unhealthy ] && {{ echo unhealthy; exit 0; }}
         case "$(phase)" in down) exit 1 ;; *) echo healthy ;; esac ;;
    esac ;;
  exec)
    target="$2"; shift 2
    case "$target" in
      id-gluetun) log "gluetun-exec"; set_phase vpn ;;
      id-qbittorrent)
        case "$(phase)" in
          up) echo "ip={VPN_IP}" ;;
          down) [ "$STUB_SCENARIO" = leak ] && {{ echo "ip={HOME_IP}"; exit 0; }}; exit 6 ;;
          recovering|vpn)
            n=$(tick)
            [ "$STUB_SCENARIO" = leak ] && [ "$n" -le 2 ] && {{ echo "ip={HOME_IP}"; exit 0; }}
            [ "$n" -le 2 ] && exit 6
            [ "$STUB_SCENARIO" = no-recovery ] && exit 6
            echo "ip={VPN_IP}" ;;
        esac ;;
      id-watchdog)
        if [ "$1" = printenv ]; then echo "$STUB_RESUME_AFTER"; exit 0; fi
        what="$5"
        case "$what" in
          state)
            case "$(phase)" in
              up) echo ok ;;
              down) [ "$STUB_SCENARIO" = blind ] && echo ok || echo down ;;
              recovering|vpn)
                n=$(tick)
                if [ "$n" -le 4 ]; then
                  [ "$STUB_SCENARIO" = blind ] && echo ok || echo down
                  if [ "$(phase)" = vpn ] && [ "$STUB_SCENARIO" != no-pause ]; then
                    echo 0 >"$st/running"
                  fi
                else
                  [ "$STUB_SCENARIO" = no-recovery ] && {{ echo down; exit 0; }}
                  echo ok; echo up >"$st/phase"
                  [ "$STUB_RESUME_AFTER" -gt 0 ] && echo 1 >"$st/running"
                fi ;;
            esac ;;
          counts) r=$(running); echo "running=$r paused=$((1 - r))" ;;
          hashes) [ "$STUB_SCENARIO" = no-torrent ] || echo {HASH} ;;
          resume) log "resume $6"; echo 1 >"$st/running"; echo ok ;;
        esac ;;
    esac ;;
  *) exit 99 ;;
esac
"""


@pytest.fixture
def run(tmp_path: Path):
    stubs = StubBin(tmp_path / "stubs")
    stubs.path.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    stubs.add("docker", DOCKER)
    stubs.add("curl", f"echo {HOME_IP}")

    def go(*args: str, scenario: str = "ok", resume_after: int = 0) -> ScriptResult:
        (state / "phase").write_text("up")
        (state / "running").write_text("0" if scenario == "no-torrent" else "1")
        return run_script(
            SCRIPT,
            list(args),
            stubs=stubs,
            env={
                "STUB_STATE": str(state),
                "STUB_SCENARIO": scenario,
                "STUB_RESUME_AFTER": str(resume_after),
                "AB_POLL_SECONDS": "0",
                "AB_PROBE_INTERVAL": "0.01",
                "AB_DETECT_TIMEOUT": "2",
                "AB_HEALTHY_TIMEOUT": "2",
                "AB_RECOVERY_TIMEOUT": "3",
            },
            timeout=60,
        )

    go.log = lambda: (state / "log").read_text().splitlines() if (state / "log").exists() else []
    return go


# --- a healthy stack passes ----------------------


def test_a_healthy_stack_passes_every_mode(run):
    result = run("--cycles", "4")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: 4 cycle(s)" in result.stdout
    for mode in ("stop", "kill", "restart", "vpn"):
        assert f": mode {mode}" in result.stdout
    assert result.stdout.count("FAIL") == 0


def test_each_mode_does_what_it_says(run):
    run("--cycles", "4")
    log = run.log()
    assert any(line.startswith("stop id-gluetun") for line in log)
    assert "kill id-gluetun" in log
    assert "restart" in log
    assert "gluetun-exec" in log, "the vpn mode kills openvpn inside gluetun"


def test_modes_rotate_over_the_cycles(run):
    result = run("--cycles", "5", "--modes", "stop,vpn")
    rows = [line.split()[1] for line in result.stdout.splitlines() if line[:1].isdigit()]
    assert rows == ["stop", "vpn", "stop", "vpn", "stop"]


def test_gluetun_is_started_again_after_stop_and_kill(run):
    run("--cycles", "2", "--modes", "stop,kill")
    log = run.log()
    assert log.count("start id-gluetun") == 2


def test_the_summary_table_has_a_row_per_cycle(run):
    result = run("--cycles", "4")
    table = result.stdout.split("cycle  mode")[1]
    assert len([line for line in table.splitlines() if line.strip().endswith("PASS")]) == 4


def test_the_vpn_mode_checks_that_torrents_were_paused_and_stay_paused_without_auto_resume(run):
    result = run("--cycles", "1", "--modes", "vpn")
    assert "all torrents paused within" in result.stdout
    assert "torrents stay paused (WATCHDOG_RESUME_AFTER=0); resuming them now" in result.stdout
    assert run.log().count(f"resume {HASH}") == 2, "the test puts back what the watchdog paused"


def test_with_auto_resume_the_torrents_must_be_running_again_by_themselves(run):
    result = run("--cycles", "1", "--modes", "vpn", resume_after=2)
    assert result.returncode == 0, result.stdout
    assert "torrents resumed by the watchdog (WATCHDOG_RESUME_AFTER=2)" in result.stdout
    # Only the exit safety net resumed anything; the cycle itself did not need to.
    assert run.log().count(f"resume {HASH}") == 1


def test_container_modes_do_not_claim_a_pause_they_cannot_make(run):
    result = run("--cycles", "1", "--modes", "stop")
    assert "all torrents paused" not in result.stdout
    row = next(ln for ln in result.stdout.splitlines() if ln.startswith("1 "))
    assert "n/a" in row


# --- failures are caught ----------------------


@pytest.mark.security
def test_a_home_address_seen_during_the_outage_fails_the_test(run):
    result = run("--cycles", "1", "--modes", "stop", scenario="leak")
    assert result.returncode == 1
    assert "HOME IP LEAK" in result.stdout
    assert HOME_IP not in result.stdout + result.stderr


def test_a_watchdog_that_never_notices_fails_the_test(run):
    result = run("--cycles", "1", "--modes", "stop", scenario="blind")
    assert result.returncode == 1
    assert "did not turn 'down' within 2s" in result.stdout


def test_torrents_that_are_not_paused_in_vpn_mode_fail_the_test(run):
    result = run("--cycles", "1", "--modes", "vpn", scenario="no-pause")
    assert result.returncode == 1
    assert "torrents were still running" in result.stdout


def test_a_stack_that_does_not_recover_fails_the_test_and_names_the_manual_fix(run):
    result = run("--cycles", "1", "--modes", "stop", scenario="no-recovery")
    assert result.returncode == 1
    assert "did not return to 'ok'" in result.stdout
    assert "docker restart qbittorrent" in result.stdout


def test_one_failed_cycle_fails_the_run_but_the_others_still_run(run):
    result = run("--cycles", "2", "--modes", "stop,vpn", scenario="no-pause")
    assert result.returncode == 1
    assert result.stdout.count(": mode ") == 2
    table = result.stdout.split("cycle  mode")[1]
    assert "PASS" in table
    assert "FAIL" in table


# --- refusals ----------------------


def test_it_refuses_to_start_when_gluetun_is_unhealthy(run):
    result = run("--cycles", "1", scenario="unhealthy")
    assert result.returncode == 2
    assert "gluetun is not healthy" in result.stderr
    assert not any(line.startswith(("stop", "kill", "restart")) for line in run.log())


def test_it_refuses_without_a_running_torrent_unless_allowed(run):
    result = run("--cycles", "1", scenario="no-torrent")
    assert result.returncode == 2
    assert "no torrent is running" in result.stderr
    allowed = run("--cycles", "1", "--modes", "stop", "--allow-no-torrent", scenario="no-torrent")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


@pytest.mark.parametrize(
    "args", [["--cycles", "0"], ["--cycles", "x"], ["--modes", "explode"], ["--bogus"]]
)
def test_bad_arguments_exit_2_without_touching_anything(run, args):
    result = run(*args)
    assert result.returncode == 2
    assert run.log() == []


def test_dry_run_changes_nothing_and_lists_the_plan(run):
    result = run("--dry-run", "--cycles", "4")
    assert result.returncode == 0
    assert run.log() == []
    for text in (
        "docker stop gluetun",
        "docker kill gluetun",
        "docker restart gluetun",
        "kill -9 openvpn",
    ):
        assert text in result.stdout


def test_help_is_the_header(run):
    result = run("--help")
    assert result.returncode == 0
    assert "vpn-abuse-test.sh: break the VPN over and over" in result.stdout
    assert "set -uo" not in result.stdout


@pytest.mark.security
def test_no_address_is_ever_printed(run):
    for scenario in ("ok", "leak"):
        result = run("--cycles", "2", "--modes", "stop,vpn", scenario=scenario)
        assert HOME_IP not in result.stdout + result.stderr
        assert VPN_IP not in result.stdout + result.stderr
