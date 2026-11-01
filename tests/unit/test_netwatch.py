"""scripts/qbittorrent/netwatch.sh: detect qbittorrent's dead network namespace."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.shell import ScriptResult, run_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qbittorrent" / "netwatch.sh"

HEADER = (
    "Inter-|   Receive                            |  Transmit\n"
    " face |bytes    packets errs drop fifo frame |bytes    packets errs drop fifo colls\n"
)
ROW = "{name}: 1234 10 0 0 0 0 0 0 4321 12 0 0 0 0 0 0\n"


def net_dev(*names: str) -> str:
    return HEADER + "".join(ROW.format(name=f"{n:>6}") for n in names)


@pytest.fixture
def run(tmp_path: Path):
    """Run netwatch against a fake /proc/net/dev; a halt writes a marker file."""
    dev = tmp_path / "net_dev"
    marker = tmp_path / "halted"

    def go(*args: str, content: str | None = None, **env: str) -> ScriptResult:
        if content is not None:
            dev.write_text(content)
        return run_script(
            SCRIPT,
            list(args),
            env={
                "NETWATCH_NET_DEV": str(dev),
                "NETWATCH_INTERVAL": "0",
                "NETWATCH_FAILS": "3",
                "NETWATCH_MAX_LOOPS": "10",
                "NETWATCH_HALT": f"touch {marker}",
                **env,
            },
        )

    go.dev = dev
    go.halted = marker
    return go


@pytest.mark.parametrize("names", [("lo", "eth0"), ("lo", "eth0", "tun0"), ("lo", "tun0")])
def test_check_passes_with_any_interface_besides_lo(run, names):
    assert run("--check", content=net_dev(*names)).returncode == 0


@pytest.mark.parametrize("content", [net_dev("lo"), net_dev(), HEADER, ""])
def test_check_fails_when_only_lo_is_left(run, content):
    assert run("--check", content=content).returncode == 1


def test_check_fails_when_the_file_is_unreadable(run, tmp_path):
    result = run("--check", NETWATCH_NET_DEV=str(tmp_path / "missing"))
    assert result.returncode == 1


def test_a_dead_namespace_stops_the_container_after_three_checks(run):
    result = run(content=net_dev("lo"))
    assert result.returncode == 0
    assert run.halted.exists()
    assert "check 3 of 3" in result.stdout
    assert "Docker restarts it" in result.stdout


def test_it_does_not_halt_before_the_threshold(run):
    result = run(content=net_dev("lo"), NETWATCH_FAILS="3", NETWATCH_MAX_LOOPS="2")
    assert not run.halted.exists()
    assert "check 2 of 3" in result.stdout


def test_a_working_namespace_is_never_halted(run):
    result = run(content=net_dev("lo", "eth0", "tun0"))
    assert result.returncode == 0
    assert not run.halted.exists()
    assert "no network interface" not in result.stdout


def test_a_down_tunnel_is_not_a_dead_namespace(run):
    # tun0 gone but eth0 there: gluetun is reconnecting and its firewall is the killswitch.
    run(content=net_dev("lo", "eth0"))
    assert not run.halted.exists()


@pytest.mark.parametrize("arg", ["custom-svc-netwatch", "anything", "--bogus"])
def test_any_argument_but_check_means_watch_mode(run, arg):
    """Regression: s6 starts the service as `run custom-svc-netwatch`. The script used to
    exit 2 on that argument, so s6 restarted it every second and it never watched."""
    result = run(arg, content=net_dev("lo"))
    assert result.returncode == 0
    assert run.halted.exists(), "it must run the watch loop, not exit on the argument"
    assert "usage" not in result.stdout + result.stderr


def test_s6_style_call_with_a_working_namespace_keeps_watching(run):
    result = run("custom-svc-netwatch", content=net_dev("lo", "eth0"), NETWATCH_MAX_LOOPS="3")
    assert result.returncode == 0
    assert not run.halted.exists()


def test_the_real_proc_net_dev_has_an_interface_or_is_readable():
    # Sanity check of the default path: the parser copes with the real file format.
    result = run_script(SCRIPT, ["--check"])
    assert result.returncode in {0, 1}
