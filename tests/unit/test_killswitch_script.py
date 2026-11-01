"""scripts/vpn/killswitch-test.sh against a stub docker: verdicts, restore, no address printed."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.shell import ScriptResult, StubBin, run_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vpn" / "killswitch-test.sh"
HOME_IP = "203.0.113.9"
VPN_IP = "198.51.100.5"

# A fake docker whose qbittorrent sees the internet depending on gluetun's state.
# STUB_SCENARIO: ok | leak-when-stopped | tunnel-leak | no-recovery | not-running
DOCKER = f"""
st="$STUB_STATE"
phase() {{ cat "$st/phase" 2>/dev/null || echo up; }}
case "$1" in
  compose)
    [ "$STUB_SCENARIO" = not-running ] && exit 0
    echo "id-${{@: -1}}" ;;
  stop) echo down >"$st/phase"; echo "stop $2" >>"$st/log" ;;
  start) echo recovering >"$st/phase"; echo 0 >"$st/n"; echo "start $2" >>"$st/log" ;;
  inspect) echo healthy ;;
  exec)
    echo "exec" >>"$st/log"
    case "$(phase)" in
      up)
        [ "$STUB_SCENARIO" = tunnel-leak ] && {{ echo {HOME_IP}; exit 0; }}
        echo {VPN_IP} ;;
      down)
        [ "$STUB_SCENARIO" = leak-when-stopped ] && {{ echo {HOME_IP}; exit 0; }}
        exit 6 ;;
      recovering)
        [ "$STUB_SCENARIO" = no-recovery ] && exit 6
        n=$(( $(cat "$st/n") + 1 )); echo "$n" >"$st/n"
        [ "$n" -le 2 ] && exit 6
        echo {VPN_IP} ;;
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

    def go(*args: str, scenario: str = "ok", home: str = HOME_IP) -> ScriptResult:
        stubs.add("curl", f'[ -n "{home}" ] && echo {home}; exit 0')
        return run_script(
            SCRIPT,
            list(args),
            stubs=stubs,
            env={
                "STUB_STATE": str(state),
                "STUB_SCENARIO": scenario,
                "KS_POLL_SECONDS": "0",
                "KS_RECOVERY_TIMEOUT": "2",
                "KS_HEALTHY_TIMEOUT": "2",
            },
        )

    go.log = lambda: (state / "log").read_text().splitlines() if (state / "log").exists() else []
    return go


def test_a_working_stack_passes_all_three_steps(run):
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS") >= 3
    assert "OK: the killswitch holds and qbittorrent recovers" in result.stdout
    assert run.log().count("stop id-gluetun") == 1
    assert run.log().count("start id-gluetun") == 1


@pytest.mark.security
@pytest.mark.parametrize("scenario", ["ok", "leak-when-stopped", "tunnel-leak", "no-recovery"])
def test_no_address_is_ever_printed(run, scenario):
    result = run(scenario=scenario)
    assert HOME_IP not in result.stdout + result.stderr
    assert VPN_IP not in result.stdout + result.stderr


@pytest.mark.security
def test_a_leak_while_gluetun_is_stopped_fails_and_gluetun_is_started_again(run):
    result = run(scenario="leak-when-stopped")
    assert result.returncode == 1
    assert "HOME IP LEAK" in result.stdout
    assert "expected no connection" in result.stdout
    assert run.log().count("start id-gluetun") == 1


@pytest.mark.security
def test_the_home_address_through_the_tunnel_container_fails_step_1(run):
    result = run(scenario="tunnel-leak")
    assert result.returncode == 1
    assert "HOME IP LEAK (expected VPN IP)" in result.stdout


def test_missing_recovery_fails_and_names_the_manual_fix(run):
    result = run(scenario="no-recovery")
    assert result.returncode == 1
    assert "docker restart qbittorrent" in result.stdout
    assert "no connection" in result.stdout


def test_recovery_needs_no_restart_of_qbittorrent(run):
    run()
    assert not [line for line in run.log() if line.startswith(("restart", "stop id-qbittorrent"))]


def test_a_stack_that_is_not_running_is_refused_before_anything_is_stopped(run):
    result = run(scenario="not-running")
    assert result.returncode == 2
    assert "start the stack first" in result.stderr
    assert not [line for line in run.log() if line.startswith("stop")]


def test_without_a_home_address_nothing_is_stopped(run):
    result = run(home="")
    assert result.returncode == 2
    assert "home address" in result.stderr
    assert not [line for line in run.log() if line.startswith("stop")]


def test_dry_run_changes_nothing(run):
    result = run("--dry-run")
    assert result.returncode == 0
    assert "docker stop" in result.stdout
    assert run.log() == []


def test_unknown_arguments_are_rejected(run):
    assert run("--bogus").returncode == 2
