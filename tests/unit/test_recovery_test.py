# ruff: noqa: E501
"""scripts/ops/recovery_test.py: judging a reboot, against a fake Docker and a real database (#32)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "recovery_test.py"
spec = importlib.util.spec_from_file_location("ytp_recovery_test", SCRIPT)
assert spec is not None
assert spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["ytp_recovery_test"] = mod
spec.loader.exec_module(mod)

sys.path.insert(0, str(ROOT / "app"))
from ytp.db import Database  # noqa: E402

HOME = "84.150.20.30"
VPN = "185.107.12.34"
T0 = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)


def stamp(delta_seconds: float) -> str:
    return (T0 + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def info(started: float, health="healthy", running=True, policy="unless-stopped"):
    state = {
        "Running": running,
        "Status": "running" if running else "exited",
        "StartedAt": stamp(started),
    }
    if health:
        state["Health"] = {"Status": health}
    return {"State": state, "HostConfig": {"RestartPolicy": {"Name": policy}}}


# Start times after the reboot: gluetun first, qbittorrent after it is healthy, app, then proxy.
AFTER = {"gluetun": 100, "qbittorrent": 140, "app": 110, "proxy": 130, "watchdog": 120}
BEFORE = {"gluetun": -5000, "qbittorrent": -4990, "app": -4980, "proxy": -4970, "watchdog": -4960}
# gluetun 3.41's real wording (it never says "healthy"): tunnel up, then DNS ready.
HEALTHY_LOG = (
    f"{stamp(90)} INFO [vpn] connecting\n"
    f"{stamp(120)} INFO [healthcheck] listening on 127.0.0.1:9999\n"
    f"{stamp(130)} INFO [openvpn] Initialization Sequence Completed\n"
    f"{stamp(135)} INFO [dns] ready\n"
)


class FakeDocker:
    def __init__(self, started=None, **overrides):
        self.infos = {s: info(t) for s, t in (started or AFTER).items()}
        self.overrides = overrides
        self.logs_text = HEALTHY_LOG
        self.watchdog_state = "ok"
        self.running_torrents = 1
        self.trace = f"fl=1\nip={VPN}\nts=1\n"
        self.enabled = True
        self.calls = 0
        self.on_inspect = None

    def container(self, service):
        return service if service in self.infos else None

    def inspect(self, ident):
        self.calls += 1
        if self.on_inspect:
            self.on_inspect(self)
        return self.infos.get(ident)

    def exec(self, ident, command, timeout=20.0):
        if ident == "watchdog" and command[-1] == "state":
            return (
                (0, self.watchdog_state)
                if self.watchdog_state != "unreachable"
                else (1, "unreachable")
            )
        if ident == "watchdog" and command[-1] == "counts":
            return 0, f"running={self.running_torrents} paused=0"
        if ident == "qbittorrent":
            return (0, self.trace) if self.trace is not None else (28, "")
        return 1, ""

    def logs(self, ident):
        return self.logs_text

    def docker_enabled(self):
        return self.enabled


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "app.db"
    Database.open(path).close()
    return path


def add_job(db_path, ext, status="queued", job_type="magnet", error=None, updated=None):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO jobs (type, external_id, metadata, status, error) VALUES (?, ?, '{}', ?, ?)",
        (job_type, ext, status, error),
    )
    if updated:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE external_id = ?", (updated, ext))
    conn.commit()
    conn.close()


def set_job(db_path, ext, status, error=None, updated=None):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE jobs SET status = ?, error = ? WHERE external_id = ?", (status, error, ext)
    )
    if updated:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE external_id = ?", (updated, ext))
    conn.commit()
    conn.close()


def state_file(tmp_path, jobs, boot="boot-1", started=None) -> Path:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "created": "2026-11-01T07:00:00Z",
                "boot_id": boot,
                "containers": {s: {"started_at": stamp(t)} for s, t in (started or BEFORE).items()},
                "jobs": jobs,
            }
        )
    )
    return path


def run_verify(
    tmp_path, db_path, docker, jobs=None, *extra, boot="boot-2", home=HOME, on_sleep=None
):
    """Run `verify` with a fake clock (sleeping just advances it) and the given facts."""
    path = state_file(tmp_path, jobs if jobs is not None else [])
    args = [
        "verify",
        "--state",
        str(path),
        "--db",
        str(db_path),
        "--wait",
        "30",
        "--poll",
        "1",
        *extra,
    ]
    clock = {"now": 0.0, "sleeps": 0}

    def sleep(seconds):
        clock["now"] += seconds
        clock["sleeps"] += 1
        if on_sleep:
            on_sleep(clock["sleeps"])

    original = mod.verify

    def patched(a, d):
        return original(
            a,
            d,
            clock=lambda: clock["now"],
            sleep=sleep,
            now=lambda: T0 + timedelta(seconds=clock["now"] + 1000),
            current_boot=lambda: boot,
            ip_of_home=lambda: home,
        )

    mod.verify = patched
    try:
        return mod.main(args, docker=docker)
    finally:
        mod.verify = original


JOBS_BEFORE = [
    {
        "id": 1,
        "type": "magnet",
        "status": "downloading",
        "error": "",
        "updated_at": "2026-11-01T07:00:00.000Z",
    }
]


def lines(capsys, prefix):
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(prefix)]


# --- prepare ------------------------------------------------------------------------------------


def test_prepare_records_the_state_privately(tmp_path, db_path, capsys, monkeypatch):
    add_job(db_path, "a" * 40, "downloading")
    add_job(db_path, "b" * 40, "done")
    state = tmp_path / "reports" / "state.json"
    docker = FakeDocker(started=BEFORE)
    assert mod.main(["prepare", "--state", str(state), "--db", str(db_path)], docker=docker) == 0
    data = json.loads(state.read_text())
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert [j["status"] for j in data["jobs"]] == ["downloading"], "only jobs in progress"
    assert set(data["containers"]) == set(mod.SERVICES)
    assert "reboot" in capsys.readouterr().out.lower()
    assert "a" * 40 not in state.read_text(), "no torrent hash or name in the state"


def test_prepare_dry_run_writes_nothing(tmp_path, db_path):
    state = tmp_path / "state.json"
    assert (
        mod.main(
            ["prepare", "--state", str(state), "--db", str(db_path), "--dry-run"],
            docker=FakeDocker(),
        )
        == 0
    )
    assert not state.exists()


@pytest.mark.parametrize("broken", ["gluetun", "watchdog"])
def test_prepare_refuses_a_stack_that_is_not_healthy(tmp_path, db_path, capsys, broken):
    docker = FakeDocker()
    docker.infos[broken] = info(1, health="unhealthy")
    assert (
        mod.main(
            ["prepare", "--state", str(tmp_path / "s.json"), "--db", str(db_path)], docker=docker
        )
        == 2
    )
    assert broken in capsys.readouterr().err
    assert not (tmp_path / "s.json").exists()


def test_prepare_refuses_a_missing_service(tmp_path, db_path):
    docker = FakeDocker()
    del docker.infos["proxy"]
    assert (
        mod.main(
            ["prepare", "--state", str(tmp_path / "s.json"), "--db", str(db_path)], docker=docker
        )
        == 2
    )


def test_prepare_without_a_database_says_what_to_do(tmp_path, capsys):
    code = mod.main(
        ["prepare", "--state", str(tmp_path / "s.json"), "--db", str(tmp_path / "none.db")],
        docker=FakeDocker(),
    )
    assert code == 2
    assert "cannot open" in capsys.readouterr().err


# --- verify: the good case ---------------------------------------------------------------------


def test_a_clean_recovery_passes(tmp_path, db_path, capsys):
    add_job(db_path, "a" * 40, "downloading")
    code = run_verify(tmp_path, db_path, FakeDocker(), JOBS_BEFORE)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "FAIL" not in out
    assert "OK: the stack came back correctly" in out
    for needle in (
        "rebooted",
        "Docker is enabled",
        "gluetun started before qbittorrent",
        "VPN IP",
        "on its way again",
    ):
        assert needle in out, needle


def test_no_address_is_ever_printed(tmp_path, db_path, capsys):
    add_job(db_path, "a" * 40, "downloading")
    run_verify(tmp_path, db_path, FakeDocker(), JOBS_BEFORE)
    out = capsys.readouterr().out
    assert HOME not in out
    assert VPN not in out


# --- verify: the reboot and the containers ----------------------------------------------------------


def test_the_same_boot_id_means_no_reboot_happened(tmp_path, db_path, capsys):
    assert run_verify(tmp_path, db_path, FakeDocker(), boot="boot-1") == 1
    assert "NOT rebooted" in capsys.readouterr().out


def test_no_reboot_check_allows_a_container_only_test(tmp_path, db_path, capsys):
    code = run_verify(tmp_path, db_path, FakeDocker(), None, "--no-reboot-check", boot="boot-1")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "reboot check skipped" in out


def test_a_container_that_was_not_started_again_fails(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.infos["app"] = info(BEFORE["app"] - 1)
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "app: was not started again" in capsys.readouterr().out


def test_a_restart_policy_that_would_not_bring_it_back_fails(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.infos["qbittorrent"] = info(AFTER["qbittorrent"], policy="no")
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "restart policy is no" in capsys.readouterr().out


def test_an_unhealthy_container_fails_after_the_wait(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.infos["gluetun"] = info(AFTER["gluetun"], health="unhealthy")
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "gluetun: running but unhealthy" in capsys.readouterr().out


def test_a_container_that_becomes_healthy_while_waiting_passes(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.infos["gluetun"] = info(AFTER["gluetun"], health="starting")

    def heal(d):
        if d.calls > 12:
            d.infos["gluetun"] = info(AFTER["gluetun"], health="healthy")

    docker.on_inspect = heal
    assert run_verify(tmp_path, db_path, docker) == 0
    capsys.readouterr()


def test_a_stopped_or_missing_container_fails(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.infos["proxy"] = info(AFTER["proxy"], running=False, health=None)
    del docker.infos["watchdog"]
    assert run_verify(tmp_path, db_path, docker) == 1
    out = capsys.readouterr().out
    assert "proxy: not running" in out
    assert "watchdog: no container" in out


def test_docker_not_enabled_at_boot_is_a_warning(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.enabled = False
    assert run_verify(tmp_path, db_path, docker) == 0
    assert "WARN  could not confirm that Docker starts at boot" in capsys.readouterr().out


# --- verify: the order ---------------------------------------------------------------------------------


def test_qbittorrent_before_gluetun_fails(tmp_path, db_path, capsys):
    docker = FakeDocker(started={**AFTER, "qbittorrent": 50})
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "qbittorrent started BEFORE gluetun" in capsys.readouterr().out


def test_proxy_before_app_fails(tmp_path, db_path, capsys):
    docker = FakeDocker(started={**AFTER, "proxy": 105})
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "proxy started BEFORE app" in capsys.readouterr().out


def test_qbittorrent_started_before_gluetun_was_ready_fails(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.logs_text = f"{stamp(300)} INFO [openvpn] Initialization Sequence Completed\n{stamp(301)} INFO [dns] ready\n"
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "BEFORE gluetun's tunnel and DNS were ready" in capsys.readouterr().out


def test_the_later_of_tunnel_up_and_dns_ready_counts(tmp_path, db_path, capsys):
    """qbittorrent started between the two lines: after the tunnel, before DNS."""
    docker = FakeDocker()
    docker.logs_text = f"{stamp(130)} INFO [openvpn] Initialization Sequence Completed\n{stamp(160)} INFO [dns] ready\n"
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "BEFORE" in capsys.readouterr().out


def test_lines_from_an_earlier_run_of_the_same_container_are_ignored(tmp_path, db_path, capsys):
    """Docker keeps one log across restarts: only lines after this start count."""
    docker = FakeDocker()
    docker.logs_text = (
        f"{stamp(-9000)} INFO [openvpn] Initialization Sequence Completed\n"
        f"{stamp(-8990)} INFO [dns] ready\n"
        f"{stamp(90)} INFO [vpn] connecting\n"
        f"{stamp(130)} INFO [openvpn] Initialization Sequence Completed\n"
        f"{stamp(135)} INFO [dns] ready\n"
    )
    assert run_verify(tmp_path, db_path, docker) == 0
    assert "after gluetun's tunnel and DNS were ready" in capsys.readouterr().out


def test_colour_codes_in_the_log_do_not_hide_the_lines(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.logs_text = (
        f"{stamp(130)} \x1b[32mINFO\x1b[0m [openvpn] Initialization Sequence Completed\n"
        f"{stamp(135)} INFO [dns] ready\n"
    )
    assert run_verify(tmp_path, db_path, docker) == 0
    assert "after gluetun's tunnel and DNS were ready" in capsys.readouterr().out


def test_only_one_of_the_two_lines_is_enough(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.logs_text = f"{stamp(130)} INFO [openvpn] Initialization Sequence Completed\n"
    assert run_verify(tmp_path, db_path, docker) == 0
    assert "after gluetun's tunnel and DNS were ready" in capsys.readouterr().out


def test_the_old_healthy_wording_does_not_count_any_more(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.logs_text = f"{stamp(135)} INFO [healthcheck] healthy!\n"
    assert run_verify(tmp_path, db_path, docker) == 0
    assert "WARN  could not read from gluetun's log" in capsys.readouterr().out


def test_an_unreadable_gluetun_log_is_only_a_warning(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.logs_text = "no timestamps here\n"
    assert run_verify(tmp_path, db_path, docker) == 0
    assert "WARN  could not read from gluetun's log" in capsys.readouterr().out


# --- verify: the VPN before traffic ---------------------------------------------------------------------


def test_a_watchdog_that_is_not_ok_fails(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.watchdog_state = "down"
    docker.running_torrents = 0
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "state is down" in capsys.readouterr().out


def test_torrents_running_while_the_vpn_is_not_ok_fail(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.watchdog_state = "blind"
    docker.running_torrents = 2
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "2 torrent(s) are RUNNING while the VPN is not ok" in capsys.readouterr().out


def test_the_home_address_is_a_leak(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.trace = f"ip={HOME}\n"
    assert run_verify(tmp_path, db_path, docker) == 1
    out = capsys.readouterr().out
    assert "HOME IP LEAK" in out
    assert HOME not in out.replace("HOME IP LEAK", "")


def test_no_connection_although_the_vpn_is_up_fails(tmp_path, db_path, capsys):
    docker = FakeDocker()
    docker.trace = None
    assert run_verify(tmp_path, db_path, docker) == 1
    assert "no connection although" in capsys.readouterr().out


def test_an_unknown_home_address_is_a_warning_not_a_pass(tmp_path, db_path, capsys):
    assert run_verify(tmp_path, db_path, FakeDocker(), home="") == 0
    out = capsys.readouterr().out
    assert "WARN  could not learn the home address" in out
    assert "(VPN IP)" not in out


# --- verify: the jobs --------------------------------------------------------------------------------


def test_a_finished_or_cleanly_failed_job_passes(tmp_path, db_path, capsys):
    add_job(db_path, "a" * 40, "done")
    add_job(db_path, "b" * 40, "failed", error="the torrent is no longer in qBittorrent")
    jobs = [
        {"id": i, "type": "magnet", "status": "downloading", "error": "", "updated_at": "x"}
        for i in (1, 2)
    ]
    assert run_verify(tmp_path, db_path, FakeDocker(), jobs) == 0
    out = capsys.readouterr().out
    assert "finished: done" in out
    assert "failed cleanly, with a reason" in out


def test_a_job_that_failed_without_a_reason_fails(tmp_path, db_path, capsys):
    add_job(db_path, "a" * 40, "failed", error="")
    assert run_verify(tmp_path, db_path, FakeDocker(), JOBS_BEFORE) == 1
    assert "WITHOUT a reason" in capsys.readouterr().out


def test_a_vanished_job_fails(tmp_path, db_path, capsys):
    assert run_verify(tmp_path, db_path, FakeDocker(), JOBS_BEFORE) == 1
    assert "has vanished" in capsys.readouterr().out


def test_a_job_stuck_in_organizing_fails(tmp_path, db_path, capsys):
    add_job(db_path, "a" * 40, "organizing", updated=stamp(-5000)[:23] + "Z")
    assert run_verify(tmp_path, db_path, FakeDocker(), JOBS_BEFORE) == 1
    assert "stuck in organizing" in capsys.readouterr().out


def test_a_job_that_is_organizing_now_is_waited_for(tmp_path, db_path, capsys):
    add_job(db_path, "a" * 40, "organizing", updated=stamp(1000)[:23] + "Z")

    def finish(sleeps):
        if sleeps >= 3:
            set_job(db_path, "a" * 40, "done")

    assert run_verify(tmp_path, db_path, FakeDocker(), JOBS_BEFORE, on_sleep=finish) == 0
    assert "finished: done" in capsys.readouterr().out


def test_a_job_that_never_settles_fails_after_the_wait(tmp_path, db_path, capsys):
    add_job(db_path, "a" * 40, "organizing", updated=stamp(1000)[:23] + "Z")
    assert run_verify(tmp_path, db_path, FakeDocker(), JOBS_BEFORE) == 1
    assert "did not settle" in capsys.readouterr().out


def test_no_jobs_is_a_warning_or_with_require_jobs_a_failure(tmp_path, db_path, capsys):
    assert run_verify(tmp_path, db_path, FakeDocker(), []) == 0
    assert "WARN  no job was in progress" in capsys.readouterr().out
    assert run_verify(tmp_path, db_path, FakeDocker(), [], "--require-jobs") == 1
    assert "FAIL  no job was in progress" in capsys.readouterr().out


# --- misc ---------------------------------------------------------------------------------------------


def test_verify_without_a_state_file_is_exit_2(tmp_path, db_path, capsys):
    code = mod.main(
        ["verify", "--state", str(tmp_path / "none.json"), "--db", str(db_path)],
        docker=FakeDocker(),
    )
    assert code == 2
    assert "run `prepare`" in capsys.readouterr().err


def test_verify_dry_run_changes_nothing(tmp_path, db_path, capsys):
    assert (
        mod.main(
            ["verify", "--state", str(tmp_path / "x.json"), "--db", str(db_path), "--dry-run"],
            docker=FakeDocker(),
        )
        == 0
    )
    assert "dry run" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-01T08:00:00.123456789Z", datetime(2026, 11, 1, 8, 0, 0, 123456, tzinfo=UTC)),
        ("2026-11-01T08:00:00Z", datetime(2026, 11, 1, 8, 0, tzinfo=UTC)),
        ("0001-01-01T00:00:00Z", None),
        ("", None),
        ("garbage", None),
    ],
)
def test_docker_times(text, expected):
    assert mod.parse_time(text) == expected


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
