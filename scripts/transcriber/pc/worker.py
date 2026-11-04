#!/usr/bin/env python3
"""worker.py: the Whisper worker on the transcription PC (docs/transcription.md).

Runs as the systemd service `ytp-transcriber` on the Linux the PC boots from the network. It asks
the app for videos without subtitles, one at a time, and for each one:

1. lets ffmpeg read the video straight from the app (`media_url`, a per-attempt link with range
   requests, so a 4 GB video is not copied first) and turn the sound into 16 kHz mono WAV in the
   work folder (a tmpfs: about 115 MB per hour of video);
2. runs faster-whisper on the GPU (default `large-v3-turbo`, int8 weights with float16 compute,
   about 2-3 GB of VRAM, many times faster than real time on an RTX 2080 Super), with the
   voice-activity filter on and without conditioning on the previous text, the two settings that
   stop Whisper from inventing text in silence and music and from repeating itself;
3. sends the SRT back; the app files it next to the video and Jellyfin shows it.

**Switching off.** When there is nothing left to do it exits with code 77, and the unit's
`ExecStopPost` powers the PC off (only for 77: stopping the service by hand never does). Nothing
left means: no video queued or running, for `IDLE_SHUTDOWN_SECONDS` (5 min); while YouTube
downloads that may still need Whisper are in progress, it waits for them up to
`WAIT_FOR_DOWNLOADS_SECONDS` (30 min). If the app cannot be reached at all for
`UNREACHABLE_SHUTDOWN_SECONDS` (15 min), it switches off too, so a PC never runs all night for
nothing. It does NOT switch off while:

* the system was not booted from the network (`NETBOOT_ONLY=1`, the default: on a normal
  installation the worker never powers anything off), see `NETBOOT_MARKERS`;
* someone is logged in (`who` lists a session: an SSH login, a desktop);
* `/run/ytp-transcriber-keep-awake` exists (`sudo touch` it to keep the PC up);
* `BUSY_COMMAND` (optional) exits 0, e.g. `pgrep -x aider` for the coding agent on the same image.

Settings come from the environment (`/etc/ytp-transcriber.env`, see transcriber.env.example).
Standard library only, plus `faster_whisper` and the `ffmpeg` program.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("ytp-transcriber")

EXIT_POWEROFF = 77  # the unit powers the PC off for exactly this code
KEEP_AWAKE_FILE = Path("/run/ytp-transcriber-keep-awake")
HEARTBEAT_SECONDS = 30.0
RESULT_TRIES = 5
FFMPEG_TIMEOUT = 3 * 3600
# Lines Whisper is known to invent for silence or music (it learned them from the subtitle files
# of TV broadcasts). A segment that is exactly one of these, or starts like the first group, is
# dropped. Kept short on purpose: a person may really say "thanks for watching" with more words.
HALLUCINATION_PREFIXES = (
    "untertitel im auftrag des zdf",
    "untertitelung im auftrag des zdf",
    "untertitel der amara.org-community",
    "untertitelung aufgrund der amara.org-community",
    "untertitel von stephanie geiges",
    "subtitles by the amara.org community",
)
HALLUCINATIONS = (
    "copyright wdr 2021",
    "swr 2020",
    "thanks for watching",
    "thank you for watching",
    "vielen dank fürs zuschauen",
    "danke fürs zuschauen",
)


class Unreachable(Exception):
    """The app did not answer (VM down, network down, proxy down)."""


class Released(Exception):
    """The app took the video back (the attempt timed out); stop working on it."""


@dataclass(frozen=True)
class Config:
    app_url: str
    token: str
    worker: str
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    model_dir: str = ""
    beam_size: int = 5
    work_dir: str = ""
    poll_seconds: float = 30.0
    idle_seconds: float = 300.0
    wait_for_downloads_seconds: float = 1800.0
    unreachable_seconds: float = 900.0
    shutdown: bool = True
    netboot_only: bool = True
    netboot_markers: tuple[str, ...] = ("nfsroot=", "root=/dev/nfs", "netboot=")
    busy_command: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ) if env is None else env
        problems = []

        def get(name: str, default: str = "") -> str:
            return env.get(name, "").strip() or default

        def number(name: str, default: float, low: float, high: float) -> float:
            raw = get(name, str(default))
            try:
                value = float(raw)
            except ValueError:
                problems.append(f"{name} must be a number, got {raw!r}")
                return default
            if not low <= value <= high:
                problems.append(f"{name} must be between {low:g} and {high:g}")
            return value

        def flag(name: str, default: bool) -> bool:
            raw = get(name, "1" if default else "0").lower()
            if raw not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
                problems.append(f"{name} must be 0 or 1, got {raw!r}")
            return raw in {"1", "true", "yes", "on"}

        app_url = get("YTP_APP_URL").rstrip("/")
        if not re.fullmatch(r"https?://[^/\s]+", app_url):
            problems.append(
                "YTP_APP_URL must be the app's address, like http://192.168.178.20:8080"
            )
        token = get("TRANSCRIBER_TOKEN")
        if len(token) < 32:
            problems.append(
                "TRANSCRIBER_TOKEN must be the value of TRANSCRIBER_TOKEN in the app's .env"
            )
        worker = re.sub(r"[^A-Za-z0-9._-]", "-", get("WORKER_NAME", socket.gethostname()))[:64]
        markers = tuple(
            m.strip()
            for m in get("NETBOOT_MARKERS", "nfsroot=,root=/dev/nfs,netboot=").split(",")
            if m.strip()
        )
        config = cls(
            app_url=app_url,
            token=token,
            worker=worker or "transcriber",
            model=get("WHISPER_MODEL", "large-v3-turbo"),
            device=get("WHISPER_DEVICE", "cuda"),
            compute_type=get("WHISPER_COMPUTE_TYPE", "int8_float16"),
            model_dir=get("WHISPER_MODEL_DIR"),
            beam_size=int(number("WHISPER_BEAM_SIZE", 5, 1, 10)),
            work_dir=get("WORK_DIR"),
            poll_seconds=number("POLL_SECONDS", 30, 5, 600),
            idle_seconds=number("IDLE_SHUTDOWN_SECONDS", 300, 0, 86400),
            wait_for_downloads_seconds=number("WAIT_FOR_DOWNLOADS_SECONDS", 1800, 0, 86400),
            unreachable_seconds=number("UNREACHABLE_SHUTDOWN_SECONDS", 900, 60, 86400),
            shutdown=flag("SHUTDOWN_WHEN_IDLE", True),
            netboot_only=flag("NETBOOT_ONLY", True),
            netboot_markers=markers,
            busy_command=get("BUSY_COMMAND"),
        )
        if problems:
            raise SystemExit("ytp-transcriber: invalid settings:\n  " + "\n  ".join(problems))
        return config


# ---------------------------------------------------------------------------------- the app


@dataclass(frozen=True)
class Job:
    id: int
    attempt: int
    language: str | None
    name: str
    media_url: str  # absolute


class AppClient:
    """The app's /api/transcriber/* routes (ytp.transcriber_api)."""

    def __init__(self, config: Config, *, timeout: float = 30.0):
        self.config = config
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.config.app_url + path,
            data=data,
            method=method,
            headers={
                "X-Transcriber-Token": self.config.token,
                "Content-Type": "application/json",
                "User-Agent": "ytp-transcriber",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("detail")
            except ValueError:
                detail = raw[:200].decode(errors="replace")
            return exc.code, {"detail": detail}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise Unreachable(str(exc)) from exc

    def _expect(self, status: int, answer: Any, *ok: int) -> Any:
        if status in ok:
            return answer
        if status in (502, 503, 504):
            raise Unreachable(f"the app answered {status}")
        detail = (answer or {}).get("detail") if isinstance(answer, dict) else answer
        raise RuntimeError(f"the app answered {status}: {detail}")

    def status(self) -> dict[str, Any]:
        return self._expect(*self._call("GET", "/api/transcriber/status"), 200)

    def claim(self) -> Job | None:
        status, answer = self._call(
            "POST", "/api/transcriber/claim", {"worker": self.config.worker}
        )
        if status == 204:
            return None
        answer = self._expect(status, answer, 200)
        return Job(
            id=int(answer["id"]),
            attempt=int(answer.get("attempt") or 1),
            language=answer.get("language"),
            name=str(answer.get("name") or answer["id"]),
            media_url=self.config.app_url + answer["media_url"],
        )

    def heartbeat(self, job: Job, progress: int) -> None:
        status, answer = self._call(
            "POST", f"/api/transcriber/jobs/{job.id}/heartbeat", {"progress": progress}
        )
        if status == 409:
            raise Released("the app took this video back")
        self._expect(status, answer, 200)

    def result(self, job: Job, language: str, srt: str) -> dict[str, Any]:
        return self._expect(
            *self._call(
                "POST", f"/api/transcriber/jobs/{job.id}/result", {"language": language, "srt": srt}
            ),
            200,
        )

    def fail(self, job: Job, error: str) -> None:
        self._expect(
            *self._call("POST", f"/api/transcriber/jobs/{job.id}/fail", {"error": error[:2000]}),
            200,
            409,
        )


# ---------------------------------------------------------------------------------- SRT


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def timestamp(seconds: float) -> str:
    """SRT time: `HH:MM:SS,mmm`."""
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def is_hallucination(text: str) -> bool:
    cleaned = " ".join(re.sub(r"[^\w\s.-]", " ", text.lower()).split()).strip(" .")
    return cleaned in HALLUCINATIONS or any(
        cleaned == prefix or cleaned.startswith(prefix + " ") for prefix in HALLUCINATION_PREFIXES
    )


def to_srt(segments: Iterable[Segment]) -> str:
    """Numbered cues; empty and invented lines dropped, overlaps and zero lengths fixed."""
    cues = []
    previous_end = 0.0
    for segment in segments:
        text = " ".join(segment.text.split())
        if not text or is_hallucination(text):
            continue
        start = max(segment.start, previous_end)
        end = max(segment.end, start + 0.3)
        cues.append(f"{len(cues) + 1}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n")
        previous_end = end
    return "\n".join(cues)


# ---------------------------------------------------------------------------------- the work


def extract_audio(media_url: str, target: Path, *, run=subprocess.run) -> None:
    """The video's sound as 16 kHz mono WAV (what Whisper works on), read over HTTP by ffmpeg."""
    argv = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-i", media_url, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-y", str(target),
    ]  # fmt: skip
    result = run(argv, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT, check=False)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("ffmpeg could not read the sound: " + " | ".join(tail)[:500])
    if not target.is_file() or target.stat().st_size <= 44:  # a WAV header and nothing else
        raise RuntimeError("the video has no sound track")


class Transcriber(Protocol):
    def transcribe(
        self, audio: Path, language: str | None, progress: Callable[[float], None]
    ) -> tuple[str, list[Segment]]: ...


class WhisperTranscriber:
    """faster-whisper, the model loaded once on first use (it stays in VRAM until shutdown)."""

    def __init__(self, config: Config):
        self.config = config
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(
                "loading model %s (%s, %s)",
                self.config.model,
                self.config.device,
                self.config.compute_type,
            )
            self._model = WhisperModel(
                self.config.model,
                device=self.config.device,
                compute_type=self.config.compute_type,
                download_root=self.config.model_dir or None,
            )
        return self._model

    def transcribe(
        self, audio: Path, language: str | None, progress: Callable[[float], None]
    ) -> tuple[str, list[Segment]]:
        model = self._load()
        segments, info = model.transcribe(
            str(audio),
            language=language or None,
            beam_size=self.config.beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        duration = float(getattr(info, "duration", 0) or 0)
        found = []
        for segment in segments:  # a generator: the work happens while iterating
            found.append(Segment(segment.start, segment.end, segment.text))
            if duration > 0:
                progress(min(segment.end / duration, 1.0))
        return info.language, found


@dataclass
class Worker:
    config: Config
    client: AppClient
    transcriber: Transcriber
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    extract: Callable[[str, Path], None] = extract_audio
    cmdline: Callable[[], str] = field(
        default=lambda: Path("/proc/cmdline").read_text(encoding="utf-8", errors="replace")
    )
    run_command: Callable[..., Any] = subprocess.run
    _last_busy_reason: str = ""

    # ------------------------------------------------------------------ one video

    def process(self, job: Job) -> None:
        logger.info("transcribing %s (id %s, attempt %s)", job.name, job.id, job.attempt)
        started = self.clock()
        with tempfile.TemporaryDirectory(dir=self.config.work_dir or None, prefix="ytp-") as tmp:
            audio = Path(tmp) / "audio.wav"
            try:
                self.extract(job.media_url, audio)
                last = {"at": self.clock()}

                def progress(fraction: float) -> None:
                    if self.clock() - last["at"] >= HEARTBEAT_SECONDS:
                        last["at"] = self.clock()
                        self.client.heartbeat(job, int(fraction * 100))

                self.client.heartbeat(job, 0)
                language, segments = self.transcriber.transcribe(audio, job.language, progress)
                answer = self._send_result(job, language, to_srt(segments))
            except Released:
                logger.warning("the app took %s back; leaving it", job.name)
                return
            except Unreachable:
                raise  # nothing to report it to; the app's lease puts it back in the queue
            except Exception as exc:
                reason = _reason(exc)
                logger.error("transcription of %s failed: %s", job.name, reason)
                with contextlib.suppress(Unreachable):  # the lease puts it back
                    self.client.fail(job, reason)
                return
        logger.info(
            "done: %s -> %s (%s, %.0f s)",
            job.name,
            answer.get("subtitle") or "no speech, no file",
            language,
            self.clock() - started,
        )

    def _send_result(self, job: Job, language: str, srt: str) -> dict[str, Any]:
        """The result is minutes of GPU work: a short outage of the app must not lose it."""
        for attempt in range(1, RESULT_TRIES + 1):
            try:
                return self.client.result(job, language, srt)
            except Unreachable:
                if attempt == RESULT_TRIES:
                    raise
                logger.warning("could not send the result (try %s), again in a moment", attempt)
                self.sleep(self.config.poll_seconds)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ switching off

    def busy_reason(self) -> str:
        """Why the PC must stay on although there is no work; "" = it may switch off."""
        if not self.config.shutdown:
            return "SHUTDOWN_WHEN_IDLE=0"
        if self.config.netboot_only:
            try:
                cmdline = self.cmdline()
            except OSError:
                cmdline = ""
            if not any(marker in cmdline for marker in self.config.netboot_markers):
                return "not booted from the network (NETBOOT_ONLY=1)"
        if KEEP_AWAKE_FILE.exists():
            return f"{KEEP_AWAKE_FILE} exists"
        try:
            who = self.run_command(["who"], capture_output=True, text=True, timeout=10, check=False)
            if who.stdout.strip():
                return "someone is logged in"
        except (OSError, subprocess.SubprocessError):
            pass
        if self.config.busy_command:
            try:
                result = self.run_command(
                    shlex.split(self.config.busy_command),
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode == 0:
                    return "BUSY_COMMAND says busy"
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        return ""

    def may_switch_off(self) -> bool:
        reason = self.busy_reason()
        if reason and reason != self._last_busy_reason:
            logger.info("idle, but staying on: %s", reason)
        self._last_busy_reason = reason
        return not reason

    # ------------------------------------------------------------------ the loop

    def run(self) -> int:
        """Work until there is nothing left; returns the exit code (77 = power off)."""
        logger.info("worker %s starts, app %s", self.config.worker, self.config.app_url)
        last_work = last_contact = self.clock()
        while True:
            try:
                job = self.client.claim()
                last_contact = self.clock()
                if job is not None:
                    self.process(job)
                    last_work = self.clock()
                    continue
                state = self.client.status()
            except Unreachable as exc:
                away = self.clock() - last_contact
                logger.warning("the app is not reachable (%s), for %.0f s", exc, away)
                if away >= self.config.unreachable_seconds and self.may_switch_off():
                    logger.info("the app has been away too long: switching off")
                    return EXIT_POWEROFF
                self.sleep(self.config.poll_seconds)
                continue
            idle = self.clock() - last_work
            if self.should_stop(state, idle) and self.may_switch_off():
                logger.info("nothing left to do: switching off")
                return EXIT_POWEROFF
            self.sleep(self.config.poll_seconds)

    def should_stop(self, state: dict[str, Any], idle: float) -> bool:
        """No transcription waiting or running, and either no download that may still bring one,
        or we have waited long enough for them."""
        if int(state.get("queued") or 0) or int(state.get("running") or 0):
            return False
        if idle < self.config.idle_seconds:
            return False
        if int(state.get("pending_downloads") or 0):
            return idle >= self.config.wait_for_downloads_seconds
        return True


def _reason(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    if "out of memory" in text.lower():
        return "the GPU ran out of memory (is another model loaded, e.g. Ollama?): " + text[:300]
    return text[:500]


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s"
    )
    config = Config.from_env()
    worker = Worker(config, AppClient(config), WhisperTranscriber(config))
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
