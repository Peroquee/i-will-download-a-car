"""The queue loop the download workers share (issue #75): one copy for both."""

from __future__ import annotations

from ytp.job_worker import QueueWorker
from ytp.podcast_jobs import PodcastWorker
from ytp.youtube_jobs import YoutubeWorker


def test_both_download_workers_use_the_one_queue_loop():
    for worker in (YoutubeWorker, PodcastWorker):
        assert issubclass(worker, QueueWorker)
        for name in ("wake", "run", "run_one", "_workdir", "_fail"):
            assert getattr(worker, name) is getattr(QueueWorker, name), (worker.__name__, name)


def test_the_workers_only_add_what_is_theirs():
    assert (YoutubeWorker.job_type, YoutubeWorker.label, YoutubeWorker.workdir_name) == (
        "youtube",
        "youtube",
        "youtube",
    )
    assert (PodcastWorker.job_type, PodcastWorker.label, PodcastWorker.workdir_name) == (
        "podcast",
        "podcast",
        "podcasts",
    )
    assert YoutubeWorker._process is not PodcastWorker._process
    assert YoutubeWorker._progress is not PodcastWorker._progress
