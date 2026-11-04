"""scripts/transcriber/pc/worker.py: SRT output, the work loop and when the PC switches off.

No GPU, no ffmpeg, no app: the app client, Whisper and ffmpeg are fakes; the clock is ours.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "transcriber" / "pc" / "worker.py"
spec = importlib.util.spec_from_file_location("ytp_transcriber_worker", SCRIPT)
assert spec is not None
assert spec.loader is not None
w = importlib.util.module_from_spec(spec)
sys.modules["ytp_transcriber_worker"] = w
spec.loader.exec_module(w)

TOKEN = "t" * 43
NETBOOT = "BOOT_IMAGE=vmlinuz root=/dev/nfs nfsroot=192.168.178.5:/srv/netboot/ubuntu ip=dhcp"


def config(**over):
    base = {"YTP_APP_URL": "http://192.168.178.20:8080", "TRANSCRIBER_TOKEN": TOKEN}
    return w.Config.from_env({**base, **over})


# --- settings -------------------------------------------------------------------------------------


def test_settings_defaults():
    c = config(WORKER_NAME="main pc!")
    assert (c.model, c.compute_type, c.device) == ("large-v3-turbo", "int8_float16", "cuda")
    assert c.worker == "main-pc-"
    assert c.shutdown
    assert c.netboot_only
    assert (c.idle_seconds, c.wait_for_downloads_seconds) == (300, 1800)


@pytest.mark.parametrize(
    "env",
    [
        {"YTP_APP_URL": "192.168.178.20"},
        {"YTP_APP_URL": "http://x/path"},
        {"TRANSCRIBER_TOKEN": "changeme"},
        {"IDLE_SHUTDOWN_SECONDS": "soon"},
        {"POLL_SECONDS": "1"},
        {"NETBOOT_ONLY": "maybe"},
    ],
)
def test_bad_settings_stop_the_worker_with_a_message(env):
    with pytest.raises(SystemExit, match="invalid settings"):
        config(**env)


# --- SRT ------------------------------------------------------------------------------------------


def test_timestamps():
    assert w.timestamp(0) == "00:00:00,000"
    assert w.timestamp(3725.5) == "01:02:05,500"
    assert w.timestamp(-1) == "00:00:00,000"


def test_srt_numbering_overlaps_and_empty_lines():
    srt = w.to_srt(
        [
            w.Segment(0.0, 2.5, "  Hallo   zusammen. "),
            w.Segment(2.0, 2.0, "Überlappt und ohne Länge"),
            w.Segment(5.0, 6.0, "   "),
            w.Segment(7.0, 8.0, "Ende."),
        ]
    )
    assert srt == (
        "1\n00:00:00,000 --> 00:00:02,500\nHallo zusammen.\n\n"
        "2\n00:00:02,500 --> 00:00:02,800\nÜberlappt und ohne Länge\n\n"
        "3\n00:00:07,000 --> 00:00:08,000\nEnde.\n"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Untertitel im Auftrag des ZDF für funk, 2017",
        "Untertitel der Amara.org-Community",
        "Thanks for watching!",
        "Vielen Dank fürs Zuschauen.",
    ],
)
def test_known_whisper_inventions_are_dropped(text):
    assert w.is_hallucination(text)
    assert w.to_srt([w.Segment(0, 1, text)]) == ""


@pytest.mark.parametrize(
    "text", ["Thanks for watching and see you next week!", "Das ZDF hat berichtet.", "Danke"]
)
def test_real_speech_is_kept(text):
    assert not w.is_hallucination(text)


def test_the_srt_passes_the_apps_own_check():
    sys.path.insert(0, str(ROOT / "app"))
    from ytp.transcriptions import normalise_srt

    srt = w.to_srt([w.Segment(0, 1.2, "Eins"), w.Segment(61, 62, "Zwei")])
    assert normalise_srt(srt) == srt


def test_ffmpeg_is_asked_for_16khz_mono_wav_from_the_url(tmp_path):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        Path(argv[-1]).write_bytes(b"RIFF" + b"\0" * 100)
        return SimpleNamespace(returncode=0, stderr="")

    w.extract_audio("http://app/api/transcriber/jobs/1/media?key=k", tmp_path / "a.wav", run=run)
    argv = seen["argv"]
    assert argv[argv.index("-i") + 1] == "http://app/api/transcriber/jobs/1/media?key=k"
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[argv.index("-ac") + 1] == "1"


def test_ffmpeg_failures_and_silent_videos_are_errors(tmp_path):
    def broken(argv, **kw):
        return SimpleNamespace(
            returncode=1, stderr="line1\nStream map '0:a:0' matches no streams\n"
        )

    with pytest.raises(RuntimeError, match="matches no streams"):
        w.extract_audio("http://x", tmp_path / "a.wav", run=broken)

    def header_only(argv, **kw):
        Path(argv[-1]).write_bytes(b"\0" * 44)
        return SimpleNamespace(returncode=0, stderr="")

    with pytest.raises(RuntimeError, match="no sound"):
        w.extract_audio("http://x", tmp_path / "b.wav", run=header_only)


# --- the loop -------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeApp:
    """The app's transcriber routes, scripted."""

    def __init__(self, jobs=(), *, pending=0, unreachable=False):
        self.jobs = list(jobs)
        self.pending = pending
        self.unreachable = unreachable
        self.results, self.failures, self.beats = [], [], []

    def claim(self):
        if self.unreachable:
            raise w.Unreachable("connection refused")
        return self.jobs.pop(0) if self.jobs else None

    def status(self):
        return {"queued": len(self.jobs), "running": 0, "pending_downloads": self.pending}

    def heartbeat(self, job, progress):
        self.beats.append((job.id, progress))

    def result(self, job, language, srt):
        self.results.append((job.id, language, srt))
        return {"status": "done", "subtitle": "x.de.srt"}

    def fail(self, job, error):
        self.failures.append((job.id, error))


class FakeWhisper:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def transcribe(self, audio, language, progress):
        self.calls.append((audio.name, language))
        if self.error:
            raise self.error
        progress(0.5)
        return language or "de", [w.Segment(0, 1, "Hallo")]


def job(n=1, language="de"):
    return w.Job(id=n, attempt=1, language=language, name=f"v{n}.mp4", media_url=f"http://app/{n}")


def make(app, whisper=None, *, cmdline=NETBOOT, who="", **env):
    clock = Clock()

    def run_command(argv, **kw):
        if argv == ["who"]:
            return SimpleNamespace(stdout=who, returncode=0)
        return subprocess.run(argv, **kw)

    worker = w.Worker(
        config(**env),
        app,
        whisper or FakeWhisper(),
        clock=clock,
        sleep=clock.sleep,
        extract=lambda url, target: target.write_bytes(b"wav"),
        cmdline=lambda: cmdline,
        run_command=run_command,
    )
    return worker, clock


def test_it_works_through_the_queue_then_switches_off_after_the_idle_time():
    app = FakeApp([job(1), job(2, None)])
    worker, clock = make(app)
    assert worker.run() == w.EXIT_POWEROFF
    assert [(r[0], r[1]) for r in app.results] == [(1, "de"), (2, "de")]
    assert app.results[0][2].startswith("1\n00:00:00,000 --> 00:00:01,000\nHallo")
    assert 300 <= clock.now < 400


def test_it_waits_for_downloads_that_may_still_bring_work_but_not_forever():
    worker, clock = make(FakeApp(pending=2))
    assert worker.run() == w.EXIT_POWEROFF
    assert 1800 <= clock.now < 1900


def test_a_failed_transcription_is_reported_and_the_next_one_runs():
    app = FakeApp([job(1)])
    worker, _ = make(app, FakeWhisper(RuntimeError("CUDA failed with error out of memory")))
    assert worker.run() == w.EXIT_POWEROFF
    assert app.results == []
    assert app.failures[0][0] == 1
    assert "ran out of memory" in app.failures[0][1]


def test_a_released_video_is_left_alone():
    class Releasing(FakeApp):
        def heartbeat(self, job, progress):
            raise w.Released("taken back")

    app = Releasing([job(1)])
    worker, _ = make(app)
    worker.run()
    assert app.results == []
    assert app.failures == []


def test_an_app_that_is_away_for_good_lets_the_pc_switch_off():
    worker, clock = make(FakeApp(unreachable=True))
    assert worker.run() == w.EXIT_POWEROFF
    assert 900 <= clock.now < 1000


def test_a_short_outage_does_not_lose_a_finished_result():
    class Flaky(FakeApp):
        tries = 0

        def result(self, job, language, srt):
            self.tries += 1
            if self.tries < 3:
                raise w.Unreachable("proxy restarting")
            return super().result(job, language, srt)

    app = Flaky([job(1)])
    worker, _ = make(app)
    worker.run()
    assert [r[0] for r in app.results] == [1]


def test_it_never_switches_off_a_pc_that_was_not_booted_from_the_network():
    worker, _ = make(FakeApp(), cmdline="BOOT_IMAGE=/vmlinuz root=UUID=1234 ro quiet")
    assert "not booted from the network" in worker.busy_reason()


def test_a_login_keeps_the_pc_on():
    worker, _ = make(FakeApp(), who="user pts/0 2026-11-04 03:00 (192.168.178.30)\n")
    assert worker.busy_reason() == "someone is logged in"


def test_busy_command_keeps_the_pc_on():
    worker, _ = make(FakeApp(), BUSY_COMMAND="true")
    assert worker.busy_reason() == "BUSY_COMMAND says busy"
    worker, _ = make(FakeApp(), BUSY_COMMAND="false")
    assert worker.busy_reason() == ""


def test_switching_off_can_be_turned_off():
    worker, _ = make(FakeApp(), SHUTDOWN_WHEN_IDLE="0")
    assert worker.busy_reason() == "SHUTDOWN_WHEN_IDLE=0"


@pytest.mark.parametrize(
    ("state", "idle", "stop"),
    [
        ({"queued": 1}, 9999, False),
        ({"running": 1}, 9999, False),  # a stale attempt that the lease will hand back
        ({}, 100, False),
        ({}, 300, True),
        ({"pending_downloads": 1}, 600, False),
        ({"pending_downloads": 1}, 1800, True),
    ],
)
def test_should_stop(state, idle, stop):
    worker, _ = make(FakeApp())
    assert worker.should_stop(state, idle) is stop


def test_the_unit_powers_off_only_for_exit_code_77():
    unit = (ROOT / "scripts/transcriber/pc/ytp-transcriber.service").read_text()
    assert "SuccessExitStatus=77" in unit
    assert "RestartPreventExitStatus=77" in unit
    assert "ExecStopPost=+/opt/ytp-transcriber/poweroff-if-done.sh" in unit
    hook = ROOT / "scripts/transcriber/pc/poweroff-if-done.sh"
    for status, powers_off in (("77", True), ("0", False), ("TERM", False), ("1", False)):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'systemctl() {{ echo "SYSTEMCTL $*"; }}; export -f systemctl; "{hook}"',
            ],
            env={"EXIT_STATUS": status, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert ("SYSTEMCTL --no-block poweroff" in result.stdout) is powers_off
