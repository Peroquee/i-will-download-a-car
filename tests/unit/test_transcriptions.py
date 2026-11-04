"""The Whisper transcription queue (docs/transcription.md): `ytp.transcriptions`.

A real database and real files in a temporary DATA_ROOT; nothing else.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ytp import alertlog, jobs, transcriptions
from ytp.db import Database
from ytp.transcriptions import MAX_ATTEMPTS, TranscriptionError

SRT = (
    "1\n00:00:00,000 --> 00:00:02,500\nHallo zusammen.\n\n"
    "2\n00:00:02,500 --> 00:00:04,000\nWillkommen.\n"
)


class Library:
    def __init__(self, tmp: Path):
        self.root = tmp / "data"
        (self.root / "Youtube" / "Blender").mkdir(parents=True)
        self.db = Database.open(tmp / "app.db")

    def video(self, name="Big Buck Bunny [aqz-KE-bpKQ].mp4") -> Path:
        path = self.root / "Youtube" / "Blender" / name
        path.write_bytes(b"video")
        return path

    def queued(self, name="Big Buck Bunny [aqz-KE-bpKQ].mp4", language="de"):
        video = self.video(name)
        assert transcriptions.enqueue(self.db, self.root, video, language=language)
        return video

    def claim(self):
        return transcriptions.claim(self.db, self.root, "main-pc")

    def set_heartbeat(self, row_id: int, sql_offset: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE transcriptions SET heartbeat_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE id = ?",
                (sql_offset, row_id),
            )


@pytest.fixture
def lib(tmp_path) -> Library:
    return Library(tmp_path)


# --- queueing -------------------------------------------------------------------------------------


def test_a_video_is_queued_once_by_its_path_below_data_root(lib):
    video = lib.queued()
    assert transcriptions.enqueue(lib.db, lib.root, video) is True  # still waiting
    rows = lib.db.query("SELECT * FROM transcriptions")
    assert len(rows) == 1
    assert rows[0]["media_path"] == "Youtube/Blender/Big Buck Bunny [aqz-KE-bpKQ].mp4"
    assert rows[0]["language"] == "de"
    assert rows[0]["status"] == "queued"


def test_a_video_outside_data_root_is_refused(lib, tmp_path):
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="subpath"):
        transcriptions.enqueue(lib.db, lib.root, outside)


def test_a_bad_language_hint_is_dropped(lib):
    video = lib.video()
    transcriptions.enqueue(lib.db, lib.root, video, language="German!")
    assert lib.db.query("SELECT language FROM transcriptions")[0]["language"] is None


def test_a_done_video_is_not_queued_again_but_a_failed_one_is(lib):
    video = lib.queued()
    row = lib.claim()
    transcriptions.complete(lib.db, lib.root, row.id, "de", SRT)
    assert transcriptions.enqueue(lib.db, lib.root, video) is False
    with lib.db.transaction() as conn:
        conn.execute("UPDATE transcriptions SET status = 'failed', attempts = 3, error = 'x'")
    assert transcriptions.enqueue(lib.db, lib.root, video, language="en") is True
    again = transcriptions.get(lib.db, row.id)
    assert (again.status, again.attempts, again.error, again.language) == ("queued", 0, None, "en")


# --- claiming -------------------------------------------------------------------------------------


def test_claim_takes_the_oldest_and_hands_out_a_fresh_key(lib):
    lib.queued("a.mp4")
    lib.queued("b.mp4")
    first = lib.claim()
    assert first.media_path.endswith("a.mp4")
    assert (first.status, first.attempts, first.worker) == ("running", 1, "main-pc")
    assert first.media_key
    assert len(first.media_key) >= 40
    second = lib.claim()
    assert second.media_path.endswith("b.mp4")
    assert second.media_key != first.media_key
    assert lib.claim() is None


def test_claim_remembers_that_the_pc_asked(lib):
    assert transcriptions.status(lib.db)["worker_last_seen"] is None
    lib.claim()
    assert transcriptions.status(lib.db)["worker_last_seen"]


def test_a_video_that_is_gone_fails_at_claim_and_the_next_is_taken(lib):
    gone = lib.queued("gone.mp4")
    lib.queued("here.mp4")
    gone.unlink()
    row = lib.claim()
    assert row.media_path.endswith("here.mp4")
    failed = lib.db.query("SELECT * FROM transcriptions WHERE media_path LIKE '%gone.mp4'")[0]
    assert failed["status"] == "failed"
    assert "not in the library" in failed["error"]


def test_a_bad_worker_name_is_refused(lib):
    with pytest.raises(TranscriptionError):
        transcriptions.claim(lib.db, lib.root, "../../etc")


# --- streaming the media --------------------------------------------------------------------------


def test_the_media_key_opens_exactly_this_file_while_it_runs(lib):
    video = lib.queued()
    row = lib.claim()
    _, path = transcriptions.media_for(lib.db, lib.root, row.id, row.media_key)
    assert path == video.resolve()
    with pytest.raises(TranscriptionError) as wrong:
        transcriptions.media_for(lib.db, lib.root, row.id, "x" * 43)
    assert wrong.value.kind == "not_found"
    transcriptions.complete(lib.db, lib.root, row.id, "de", SRT)
    with pytest.raises(TranscriptionError):
        transcriptions.media_for(lib.db, lib.root, row.id, row.media_key)  # not running any more


def test_a_stored_path_that_leads_outside_data_root_is_never_served(lib, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("no")
    with lib.db.transaction() as conn:
        conn.execute("INSERT INTO transcriptions (media_path) VALUES ('../secret.txt')")
    assert transcriptions.media_file(lib.root, "../secret.txt") is None
    assert lib.claim() is None  # failed on the spot, not handed out
    assert lib.db.query("SELECT status FROM transcriptions")[0]["status"] == "failed"


def test_an_absolute_media_path_cannot_be_stored(lib):
    with pytest.raises(sqlite3.IntegrityError), lib.db.transaction() as conn:
        conn.execute("INSERT INTO transcriptions (media_path) VALUES ('/etc/passwd')")


# --- results --------------------------------------------------------------------------------------


def test_the_result_is_filed_next_to_the_video_for_jellyfin(lib):
    video = lib.queued()
    row = lib.claim()
    done = transcriptions.complete(lib.db, lib.root, row.id, "de", "﻿" + SRT.replace("\n", "\r\n"))
    target = video.with_name("Big Buck Bunny [aqz-KE-bpKQ].de.srt")
    assert target.read_text(encoding="utf-8") == SRT  # BOM and CRLF normalised
    assert (done.status, done.result_lang, done.progress, done.media_key) == (
        "done",
        "de",
        100,
        None,
    )
    assert done.result_path == "Youtube/Blender/Big Buck Bunny [aqz-KE-bpKQ].de.srt"
    assert not list(video.parent.glob(".*.part"))  # no temporary file left behind


def test_an_existing_subtitle_is_never_replaced(lib):
    video = lib.queued()
    mine = video.with_name("Big Buck Bunny [aqz-KE-bpKQ].de.srt")
    mine.write_text("my own\n")
    row = lib.claim()
    assert transcriptions.complete(lib.db, lib.root, row.id, "de", SRT).status == "done"
    assert mine.read_text() == "my own\n"


def test_no_speech_means_done_without_a_file(lib):
    video = lib.queued()
    row = lib.claim()
    done = transcriptions.complete(lib.db, lib.root, row.id, "en", "  \n")
    assert done.status == "done"
    assert done.result_path is None
    assert not list(video.parent.glob("*.srt"))


@pytest.mark.parametrize(
    ("language", "srt"),
    [
        ("de", "this is not a subtitle file"),
        ("de", "1\n00:00:00 --> 00:00:01\nno milliseconds\n"),
        ("de", "1\n00:00:00,000 --> 00:00:01,000\nnul\x00\n"),
        ("deutsch", SRT),
        ("../", SRT),
    ],
)
def test_a_bad_result_is_refused_and_the_row_keeps_running(lib, language, srt):
    lib.queued()
    row = lib.claim()
    with pytest.raises(TranscriptionError) as refused:
        transcriptions.complete(lib.db, lib.root, row.id, language, srt)
    assert refused.value.kind == "invalid"
    assert transcriptions.get(lib.db, row.id).status == "running"


def test_a_result_for_a_row_that_is_not_running_is_a_conflict(lib):
    lib.queued()
    with pytest.raises(TranscriptionError) as conflict:
        transcriptions.complete(lib.db, lib.root, 1, "de", SRT)
    assert conflict.value.kind == "conflict"


def test_a_too_large_result_is_refused(lib):
    lib.queued()
    row = lib.claim()
    huge = SRT + "x" * (transcriptions.MAX_SRT_BYTES + 1)
    with pytest.raises(TranscriptionError):
        transcriptions.complete(lib.db, lib.root, row.id, "de", huge)


# --- failures, retries and the lease --------------------------------------------------------------


def test_a_failure_is_retried_then_given_up_with_an_alert(lib):
    lib.queued()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        row = lib.claim()
        assert row.attempts == attempt
        after = transcriptions.fail(lib.db, row.id, "CUDA out of memory")
        expected = "queued" if attempt < MAX_ATTEMPTS else "failed"
        assert after.status == expected
        assert after.error == "CUDA out of memory"
    assert lib.claim() is None
    alerts = alertlog.list_alerts(lib.db)
    assert [a.kind for a in alerts] == ["transcription_failed"]
    assert "Big Buck Bunny" in alerts[0].title


def test_fail_for_a_row_that_is_not_running_is_a_conflict(lib):
    lib.queued()
    with pytest.raises(TranscriptionError) as conflict:
        transcriptions.fail(lib.db, 1, "x")
    assert conflict.value.kind == "conflict"


def test_heartbeats_keep_the_lease_and_record_progress(lib):
    lib.queued()
    row = lib.claim()
    lib.set_heartbeat(row.id, "-500 seconds")
    assert transcriptions.heartbeat(lib.db, row.id, 140) is True
    assert transcriptions.release_stale(lib.db) == 0
    assert transcriptions.get(lib.db, row.id).progress == 100  # clamped


def test_a_pc_that_stops_answering_loses_the_video_to_the_queue(lib):
    lib.queued()
    row = lib.claim()
    lib.set_heartbeat(row.id, "-11 minutes")
    assert transcriptions.release_stale(lib.db) == 1
    back = transcriptions.get(lib.db, row.id)
    assert (back.status, back.media_key) == ("queued", None)
    assert "stopped answering" in back.error
    assert transcriptions.heartbeat(lib.db, row.id, 50) is False  # the old attempt is over


def test_status_counts_the_queue_and_the_downloads_that_may_still_need_whisper(lib):
    lib.queued("a.mp4")
    lib.queued("b.mp4")
    lib.claim()
    for video_id, meta in [
        ("vid00000001", '{"type":"youtube","video_id":"vid00000001","channel":"c","title":"t"}'),
        (
            "vid00000002@podcast",
            '{"type":"youtube","video_id":"vid00000002","channel":"c","title":"t","target":"podcast"}',
        ),
        (
            "vid00000003@podcast",
            '{"type":"youtube","video_id":"vid00000003","channel":"c","title":"t",'
            '"target":"podcast","audio_only":true}',
        ),
        (
            "vid00000004@music",
            '{"type":"youtube","video_id":"vid00000004","channel":"c","title":"t",'
            '"target":"music","album":"a"}',
        ),
    ]:
        jobs.submit_job(lib.db, "youtube", video_id, meta)
    state = transcriptions.status(lib.db)
    assert state["queued"] == 1
    assert state["running"] == 1
    assert state["done"] == 0
    assert state["failed"] == 0
    # the plain video and the podcast kept as video; not the audio-only episode, not the track
    assert state["pending_downloads"] == 2
