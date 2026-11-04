"""scripts/transcriber/wake.py: when the transcription PC is woken, and the packet itself."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "transcriber" / "wake.py"
spec = importlib.util.spec_from_file_location("ytp_transcriber_wake", SCRIPT)
assert spec is not None
assert spec.loader is not None
wake = importlib.util.module_from_spec(spec)
sys.modules["ytp_transcriber_wake"] = wake
spec.loader.exec_module(wake)

from ytp import transcriptions  # noqa: E402
from ytp.db import Database  # noqa: E402

MAC = "aa:bb:cc:dd:ee:ff"
NOW = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)


def cfg(tmp_path, mac=MAC, hours=None):
    return wake.Config(root=tmp_path, mac=mac, address="192.168.178.255", hours=hours)


def queue(queued=1, seen_ago=None):
    seen = None if seen_ago is None else NOW - timedelta(seconds=seen_ago)
    return wake.QueueState(queued=queued, running=0, worker_last_seen=seen)


# --- settings -------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabbccddeeff"])
def test_mac_addresses_are_normalised(text):
    assert wake.parse_mac(text) == MAC


@pytest.mark.parametrize("text", ["aa:bb:cc:dd:ee", "aa:bb-cc:dd:ee:ff", "zz:bb:cc:dd:ee:ff", "1"])
def test_bad_mac_addresses_are_refused(text):
    with pytest.raises(wake.WakeError):
        wake.parse_mac(text)


def test_wake_hours_with_and_without_midnight():
    assert wake.parse_hours("") is None
    night = wake.parse_hours("22-6")
    assert [h for h in range(24) if wake.within(night, h)] == [0, 1, 2, 3, 4, 5, 22, 23]
    early = wake.parse_hours("1-7")
    assert [h for h in range(24) if wake.within(early, h)] == [1, 2, 3, 4, 5, 6]
    for bad in ("1", "7-7", "25-3", "a-b"):
        with pytest.raises(wake.WakeError):
            wake.parse_hours(bad)


def test_settings_come_from_dotenv_and_the_environment_wins(tmp_path):
    (tmp_path / ".env").write_text(
        "TRANSCRIBER_PC_MAC=AA-BB-CC-DD-EE-FF\nTRANSCRIBER_WOL_ADDRESS=192.168.178.255\n"
        "TRANSCRIBER_WAKE_HOURS=1-7\n"
    )
    loaded = wake.load_config(tmp_path, {})
    assert (loaded.mac, loaded.address, loaded.hours) == (MAC, "192.168.178.255", (1, 7))
    assert wake.load_config(tmp_path, {"TRANSCRIBER_WAKE_HOURS": "2-3"}).hours == (2, 3)
    with pytest.raises(wake.WakeError):
        wake.load_config(tmp_path, {"TRANSCRIBER_WOL_ADDRESS": "pc.lan"})


# --- deciding -------------------------------------------------------------------------------------


def test_nothing_waiting_never_wakes(tmp_path):
    assert wake.decide(cfg(tmp_path), queue(queued=0), None, NOW)[0] is False


def test_work_waiting_and_the_pc_away_wakes(tmp_path):
    assert wake.decide(cfg(tmp_path), queue(seen_ago=3600), None, NOW) == (
        True,
        "1 waiting and the PC is not up",
    )


def test_a_pc_that_asked_recently_is_not_woken(tmp_path):
    should, why = wake.decide(cfg(tmp_path), queue(seen_ago=40), None, NOW)
    assert should is False
    assert "up" in why


def test_a_packet_is_not_repeated_while_the_pc_boots(tmp_path):
    woken = NOW - timedelta(seconds=120)
    assert wake.decide(cfg(tmp_path), queue(), woken, NOW)[0] is False
    later = NOW - timedelta(seconds=wake.WAKE_RETRY_SECONDS + 1)
    assert wake.decide(cfg(tmp_path), queue(), later, NOW)[0] is True


def test_no_mac_means_no_packet(tmp_path):
    should, why = wake.decide(cfg(tmp_path, mac=""), queue(), None, NOW)
    assert should is False
    assert "TRANSCRIBER_PC_MAC" in why


def test_outside_the_wake_hours_nothing_is_sent(tmp_path):
    hour = NOW.astimezone().hour
    allowed = (hour, (hour + 1) % 24) if hour != 23 else (23, 24)
    assert wake.decide(cfg(tmp_path, hours=allowed), queue(), None, NOW)[0] is True
    other = ((hour + 2) % 24, (hour + 3) % 24)
    assert wake.decide(cfg(tmp_path, hours=other), queue(), None, NOW)[0] is False


# --- the packet and a whole run -------------------------------------------------------------------


def test_the_magic_packet():
    packet = wake.magic_packet(MAC)
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16


def make_db(root: Path) -> Database:
    (root / "config" / "app").mkdir(parents=True)
    (root / "data" / "Youtube").mkdir(parents=True)
    return Database.open(root / "config" / "app" / "app.db")


def test_a_run_reads_the_apps_database_sends_once_and_remembers_it(tmp_path):
    db = make_db(tmp_path)
    video = tmp_path / "data" / "Youtube" / "v.mp4"
    video.write_bytes(b"v")
    transcriptions.enqueue(db, tmp_path / "data", video)
    sent = []
    config = cfg(tmp_path)
    message = wake.run(config, now=NOW, send=lambda mac, address: sent.append((mac, address)))
    assert message.startswith("woke aa:bb:cc:dd:ee:ff")
    assert sent == [(MAC, "192.168.178.255")]
    assert wake.last_wake(config) == NOW
    # two minutes later: the PC is still booting, no second packet
    assert wake.run(config, now=NOW + timedelta(minutes=2), send=sent.append).startswith("not")
    assert len(sent) == 1
    # the PC's worker asks for work: it is up, nothing is sent
    other = video.with_name("w.mp4")
    other.write_bytes(b"w")
    transcriptions.claim(db, tmp_path / "data", "main-pc")
    transcriptions.enqueue(db, tmp_path / "data", other)
    state = wake.read_queue(config.database)
    assert state.queued == 1
    assert state.running == 1
    should, why = wake.decide(config, state, None, datetime.now(UTC))
    assert should is False
    assert "the PC is up" in why


def test_a_dry_run_sends_nothing(tmp_path):
    db = make_db(tmp_path)
    video = tmp_path / "data" / "Youtube" / "v.mp4"
    video.write_bytes(b"v")
    transcriptions.enqueue(db, tmp_path / "data", video)
    sent = []
    assert wake.run(cfg(tmp_path), dry_run=True, now=NOW, send=sent.append).startswith("would")
    assert sent == []
    assert wake.last_wake(cfg(tmp_path)) is None


def test_a_missing_database_is_a_readable_error(tmp_path):
    with pytest.raises(wake.WakeError, match="database"):
        wake.read_queue(tmp_path / "nope.db")


def test_the_units_are_rendered_for_the_install_folder(tmp_path, capsys):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    wake.install_units(ROOT, "media", dry_run=True, unit_dir=unit_dir)
    out = capsys.readouterr().out
    assert "systemctl enable --now youtube-premium-transcriber-wake.timer" in out
    service = (
        ROOT / "scripts/transcriber/systemd/youtube-premium-transcriber-wake.service"
    ).read_text()
    rendered = wake.render_unit(service, "media", Path("/srv/ytp"))
    assert "User=media" in rendered
    assert "ExecStart=/usr/bin/python3 /srv/ytp/scripts/transcriber/wake.py run" in rendered
    assert "@" not in rendered.replace("@ROOT@", "")
    with pytest.raises(wake.WakeError):
        wake.install_units(ROOT, "root; rm -rf /", dry_run=True, unit_dir=unit_dir)
