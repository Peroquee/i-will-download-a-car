"""Download a podcast's whole back catalogue (#45): the plan, the feeder, the API."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from tests.unit.test_podcast_jobs import ENV, FakeFetcher, FakeWorker, feed_of, pub
from tests.unit.test_podcasts import item
from ytp import accounts, alertlog, jobs, podcast_backlog
from ytp.config import Settings
from ytp.db import Database
from ytp.main import create_app
from ytp.podcast_backlog import BacklogExists, BacklogFeeder, fits, floor_bytes
from ytp.podcast_jobs import PodcastWorker
from ytp.podcasts import PodcastFeedClient, episode_key, parse_podcast

FEED = "https://podcast.example/feed.xml"
GB = 1024**3
MB = 1024**2


def episodes(count: int, size: int | None = 10 * MB, start: int = 0):
    """`count` episodes, E0 the newest (a day older each)."""
    return [
        item(
            f"g{i}",
            f"Episode {i}",
            pub(-(i + 1)),
            f"https://cdn.example/{i}.mp3",
            length=str(size) if size is not None else "",
        )
        for i in range(start, start + count)
    ]


def podcast_of(*items):
    return parse_podcast(feed_of(*items), FEED)


@pytest.fixture
def db(tmp_path) -> Iterator[Database]:
    database = Database.open(tmp_path / "app.db")
    with database.transaction() as conn:
        for number, name in ((1, "alice"), (2, "bob")):
            conn.execute(
                "INSERT INTO accounts (id, username, password_hash, role) "
                "VALUES (?, ?, 'x', 'user')",
                (number, name),
            )
    yield database
    database.close()


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "Podcasts"
    folder.mkdir()
    return folder


class Disk:
    """A drive whose free space the test sets."""

    def __init__(self, free=500 * GB, total=1000 * GB):
        self.free, self.total = free, total

    def __call__(self, path):
        return SimpleNamespace(free=self.free, total=self.total)


def settings(tmp_path) -> Settings:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    return Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config)})


def feeder(db, tmp_path, disk=None, worker=None, **kw) -> BacklogFeeder:
    return BacklogFeeder(
        db, worker or FakeWorker(), settings(tmp_path), disk_usage=disk or Disk(), **kw
    )


def run_of(db, library, tmp_path, count=5, size=10 * MB, account=1):
    podcast = podcast_of(*episodes(count, size))
    chosen = podcast_backlog.plan(db, podcast, "Test Cast", library)
    return podcast_backlog.start(db, podcast, "Test Cast", account, chosen), podcast


def finish_all(db, status="done"):
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = ? WHERE type = 'podcast' "
            "AND status IN ('queued', 'downloading', 'organizing')",
            (status,),
        )


# --- the plan ---


def test_the_plan_lists_every_episode_newest_first_with_the_known_sizes(db, library):
    chosen = podcast_backlog.plan(db, podcast_of(*episodes(4, 5 * MB)), "Test Cast", library)
    assert chosen.episodes == chosen.total == 4
    assert chosen.skipped == 0
    assert (chosen.size_bytes, chosen.sized) == (20 * MB, 4)
    keys = [i.key for i in chosen.items]
    assert keys == [episode_key(FEED, f"g{n}") for n in range(4)], "E0 is the newest"


def test_episodes_without_a_size_count_but_add_nothing(db, library):
    chosen = podcast_backlog.plan(db, podcast_of(*episodes(3, None)), "Test Cast", library)
    assert (chosen.total, chosen.size_bytes, chosen.sized) == (3, 0, 0)


@pytest.mark.parametrize("status", ["queued", "downloading", "organizing", "done"])
def test_what_is_queued_running_or_done_is_skipped(db, library, status):
    podcast = podcast_of(*episodes(3))
    job, _ = jobs.submit_job(db, "podcast", episode_key(FEED, "g1"), "{}")
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job.id))
    chosen = podcast_backlog.plan(db, podcast, "Test Cast", library)
    assert (chosen.total, chosen.skipped) == (2, 1)
    assert episode_key(FEED, "g1") not in [i.key for i in chosen.items]


def test_a_failed_episode_is_taken_again(db, library):
    podcast = podcast_of(*episodes(2))
    job, _ = jobs.submit_job(db, "podcast", episode_key(FEED, "g0"), "{}")
    jobs.mark_failed(db, job.id, "boom")
    assert podcast_backlog.plan(db, podcast, "Test Cast", library).total == 2


@pytest.mark.parametrize(
    "name", ["{d} - Episode 1.mp3", "{d} - Episode 1.m4a", "{d} - Episode 1 (2).mp3"]
)
def test_a_file_of_that_name_in_the_folder_counts_as_there(db, library, name):
    podcast = podcast_of(*episodes(3))
    published = podcast.episodes[1].published.date().isoformat()
    folder = library / "Test Cast"
    folder.mkdir()
    (folder / name.format(d=published)).write_bytes(b"x")
    chosen = podcast_backlog.plan(db, podcast, "Test Cast", library)
    assert (chosen.total, chosen.skipped) == (2, 1)


def test_other_files_do_not_hide_an_episode(db, library):
    podcast = podcast_of(*episodes(2))
    folder = library / "Test Cast"
    folder.mkdir()
    (folder / "folder.jpg").write_bytes(b"x")
    (folder / "2020-01-01 - Something else.mp3").write_bytes(b"x")
    assert podcast_backlog.plan(db, podcast, "Test Cast", library).total == 2


def test_a_catalogue_of_hundreds_is_read_completely():
    """The owner's example: 312 episodes. The old limit of 300 would have cut it."""
    many = [item(f"g{i}", f"E{i}", pub(-i - 1), f"https://cdn.example/{i}.mp3") for i in range(312)]
    assert len(parse_podcast(feed_of(*many), FEED).episodes) == 312


# --- space ---


def test_the_floor_is_five_gib_or_five_percent_of_the_drive():
    assert floor_bytes(100 * GB) == 5 * GB
    assert floor_bytes(1000 * GB) == 50 * GB


def test_an_episode_fits_if_the_floor_stays_free():
    assert fits(60 * GB, 1000 * GB, 10 * GB)
    assert not fits(55 * GB, 1000 * GB, 10 * GB)
    assert fits(8 * GB, 100 * GB, 3 * GB)
    assert not fits(7 * GB, 100 * GB, 3 * GB)


# --- the runs ---


def test_a_run_stores_its_items_and_the_owner(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=5)
    view = podcast_backlog.get(db, run_id)
    assert (view.state, view.total, view.done, view.failed, view.waiting) == ("running", 5, 0, 0, 5)
    assert podcast_backlog.owns(db, run_id, 1)
    assert not podcast_backlog.owns(db, run_id, 2)


def test_a_second_run_for_the_same_feed_joins_the_first(db, library, tmp_path):
    run_id, podcast = run_of(db, library, tmp_path)
    chosen = podcast_backlog.plan(db, podcast, "Test Cast", library)
    with pytest.raises(BacklogExists) as error:
        podcast_backlog.start(db, podcast, "Test Cast", 2, chosen)
    assert error.value.backlog_id == run_id
    assert podcast_backlog.owns(db, run_id, 2)
    assert len(podcast_backlog.visible(db)) == 1


def test_after_a_run_ended_a_new_one_can_start(db, library, tmp_path):
    run_id, podcast = run_of(db, library, tmp_path)
    podcast_backlog.cancel(db, run_id)
    chosen = podcast_backlog.plan(db, podcast, "Test Cast", library)
    assert podcast_backlog.start(db, podcast, "Test Cast", 1, chosen) != run_id


# --- the feeder ---


def test_only_a_few_episodes_are_queued_at_a_time_newest_first(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=6)
    worker = FakeWorker()
    f = feeder(db, tmp_path, worker=worker)
    for _ in range(5):
        f.tick()  # nothing finishes: it must not queue more
    queued = jobs.list_jobs(db)
    assert len(queued) == podcast_backlog.PARALLEL == 2
    assert {j.external_id for j in queued} == {episode_key(FEED, "g0"), episode_key(FEED, "g1")}
    assert worker.woken >= 1
    assert podcast_backlog.get(db, run_id).waiting == 4


def test_finished_episodes_make_room_for_the_next(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=5)
    f = feeder(db, tmp_path)
    seen = []
    for _ in range(6):
        f.tick()
        seen.append(len(jobs.list_jobs(db)))
        finish_all(db)
    assert seen[:3] == [2, 4, 5]
    view = podcast_backlog.get(db, run_id)
    assert (view.state, view.done, view.total, view.waiting) == ("done", 5, 5, 0)


def test_the_run_is_not_done_while_an_episode_is_still_running(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=2)
    f = feeder(db, tmp_path)
    f.tick()
    assert podcast_backlog.get(db, run_id).state == "running"


def test_a_failed_episode_is_counted_and_does_not_stop_the_run(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=3)
    f = feeder(db, tmp_path)
    f.tick()
    finish_all(db, "failed")
    f.tick()
    finish_all(db)
    f.tick()
    view = podcast_backlog.get(db, run_id)
    assert (view.state, view.done, view.failed) == ("done", 1, 2)


def test_other_downloads_keep_their_turn(db, library, tmp_path):
    """A single episode picked by hand during a run waits behind at most the two in the queue."""
    run_of(db, library, tmp_path, count=8)
    f = feeder(db, tmp_path)
    f.tick()
    mine, _ = jobs.submit_job(db, "podcast", episode_key(FEED, "single"), "{}")
    f.tick()
    ahead = [j for j in jobs.list_jobs(db, limit=50) if j.id < mine.id]
    assert len(ahead) == 2


def test_the_episodes_belong_to_the_asker_and_to_whoever_joined(db, library, tmp_path):
    _, podcast = run_of(db, library, tmp_path, count=4, account=1)
    f = feeder(db, tmp_path)
    f.tick()
    first = jobs.list_jobs(db)
    assert all(accounts.owns_job(db, j.id, 1) for j in first)
    assert not any(accounts.owns_job(db, j.id, 2) for j in first)
    with pytest.raises(BacklogExists):
        podcast_backlog.start(
            db, podcast, "Test Cast", 2, podcast_backlog.plan(db, podcast, "Test Cast", library)
        )
    finish_all(db)
    f.tick()
    later = [j for j in jobs.list_jobs(db) if j.id > max(x.id for x in first)]
    assert later
    assert all(accounts.owns_job(db, j.id, 1) and accounts.owns_job(db, j.id, 2) for j in later)


def test_followers_of_the_subscription_do_not_get_the_episodes(db, library, tmp_path):
    from ytp import podcast_subscriptions

    sub, _ = podcast_subscriptions.add(db, FEED, "Test Cast")
    accounts.follow(db, "podcast", sub.id, 2)
    run_of(db, library, tmp_path, account=1)
    feeder(db, tmp_path).tick()
    assert not any(accounts.owns_job(db, j.id, 2) for j in jobs.list_jobs(db))


def test_nothing_is_queued_twice(db, library, tmp_path):
    run_of(db, library, tmp_path, count=3)
    f = feeder(db, tmp_path, parallel=10)
    f.tick()
    f.tick()
    assert len(jobs.list_jobs(db)) == 3


# --- the disk ---


def test_the_run_pauses_with_an_alert_when_the_drive_is_nearly_full_and_queues_nothing(
    db, library, tmp_path, caplog
):
    run_id, _ = run_of(db, library, tmp_path, count=4)
    disk = Disk(free=51 * GB, total=1000 * GB)  # floor is 50 GB, an episode needs 10 MB: fits
    f = feeder(db, tmp_path, disk=disk)
    f.tick()
    assert len(jobs.list_jobs(db)) == 2
    finish_all(db)
    disk.free = 49 * GB
    with caplog.at_level(logging.WARNING, logger="ytp.podcast_backlog"):
        f.tick()
        f.tick()
    view = podcast_backlog.get(db, run_id)
    assert (view.state, view.paused_reason) == ("paused", "disk")
    assert len(jobs.list_jobs(db)) == 2, "nothing more was queued"
    (alert,) = alertlog.list_alerts(db)
    assert (alert.kind, alert.severity) == ("backlog_paused", "warning")
    assert alert.params["podcast"] == "Test Cast"
    assert alert.params["done"] == 2
    assert alert.params["total"] == 4
    assert "paused" in caplog.text


def test_an_episode_bigger_than_the_room_left_pauses_too(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=2, size=30 * GB)
    f = feeder(db, tmp_path, disk=Disk(free=70 * GB, total=1000 * GB))  # 70 - 30 < 50
    f.tick()
    assert jobs.list_jobs(db) == []
    assert podcast_backlog.get(db, run_id).state == "paused"


def test_the_run_goes_on_by_itself_when_there_is_room_again(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=3)
    disk = Disk(free=10 * GB, total=1000 * GB)
    f = feeder(db, tmp_path, disk=disk)
    f.tick()
    assert podcast_backlog.get(db, run_id).state == "paused"
    disk.free = 400 * GB
    f.tick()
    view = podcast_backlog.get(db, run_id)
    assert (view.state, view.paused_reason) == ("running", None)
    assert len(jobs.list_jobs(db)) == 2


def test_a_second_pause_is_a_new_alert_but_one_pause_is_one_alert(db, library, tmp_path):
    run_of(db, library, tmp_path, count=6)
    disk = Disk(free=10 * GB, total=1000 * GB)
    moments = iter(range(1, 100))
    from datetime import UTC, datetime, timedelta

    f = feeder(
        db,
        tmp_path,
        disk=disk,
        now=lambda: datetime(2026, 11, 3, tzinfo=UTC) + timedelta(minutes=next(moments)),
    )
    f.tick()
    f.tick()
    assert len(alertlog.list_alerts(db)) == 1
    disk.free = 400 * GB
    f.tick()
    finish_all(db)
    disk.free = 10 * GB
    f.tick()
    assert len(alertlog.list_alerts(db)) == 2


def test_an_unreadable_drive_waits_quietly(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path)

    def broken(path):
        raise OSError("not mounted")

    feeder(db, tmp_path, disk=broken).tick()
    assert jobs.list_jobs(db) == []
    assert podcast_backlog.get(db, run_id).state == "running"
    assert alertlog.list_alerts(db) == []


# --- cancel, clean-up ---


def test_cancelling_stops_the_queuing_and_leaves_what_is_queued(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, count=6)
    f = feeder(db, tmp_path)
    f.tick()
    assert podcast_backlog.cancel(db, run_id) is True
    finish_all(db)
    f.tick()
    assert len(jobs.list_jobs(db)) == 2
    assert podcast_backlog.get(db, run_id).state == "cancelled"
    assert podcast_backlog.cancel(db, run_id) is False


def test_old_finished_runs_are_forgotten_but_not_young_or_running_ones(db, library, tmp_path):
    done_id, podcast = run_of(db, library, tmp_path)
    podcast_backlog.cancel(db, done_id)
    chosen = podcast_backlog.plan(db, podcast, "Test Cast", library)
    running_id = podcast_backlog.start(db, podcast, "Test Cast", 1, chosen)
    with db.transaction() as conn:
        conn.execute("UPDATE podcast_backlogs SET updated_at = '2020-01-01T00:00:00.000Z'")
    feeder(db, tmp_path).tick()
    assert podcast_backlog.get(db, done_id) is None
    assert podcast_backlog.get(db, running_id) is not None
    assert (
        db.query(
            "SELECT COUNT(*) AS n FROM podcast_backlog_items WHERE backlog_id = ?", (done_id,)
        )[0]["n"]
        == 0
    )


def test_the_view_hides_old_finished_runs_but_shows_recent_ones(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path)
    podcast_backlog.cancel(db, run_id)
    assert [r.id for r in podcast_backlog.visible(db)] == [run_id]
    with db.transaction() as conn:
        conn.execute("UPDATE podcast_backlogs SET updated_at = '2020-01-01T00:00:00.000Z'")
    assert podcast_backlog.visible(db) == []
    assert [r.id for r in podcast_backlog.visible(db, finished=True)] == [run_id]


def test_deleting_an_account_removes_its_ownership_not_the_run(db, library, tmp_path):
    run_id, _ = run_of(db, library, tmp_path, account=2)
    accounts.delete(db, 2)
    assert podcast_backlog.get(db, run_id) is not None
    assert podcast_backlog.visible(db, 2) == []
    assert len(podcast_backlog.visible(db)) == 1


# --- the API ---


@pytest.fixture(autouse=False)
def api(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setattr(PodcastWorker, "run", lambda self, stop: None)
    config = tmp_path / "config"
    config.mkdir()
    (tmp_path / "Podcasts").mkdir()
    values = {
        **ENV,
        "DATA_ROOT": str(tmp_path),
        "CONFIG_DIR": str(config),
        "PODCAST_POLL_MINUTES": "0",
    }
    client = TestClient(create_app(Settings.from_env(values)), base_url="http://nas.lan")
    client.__enter__()
    fetcher = FakeFetcher()
    feeds = PodcastFeedClient(fetcher)
    client.fetcher = fetcher  # type: ignore[attr-defined]
    client.app.state.podcast_feeds = feeds
    client.disk = Disk()  # type: ignore[attr-defined]
    client.app.state.backlog_feeder._disk_usage = client.disk
    monkeypatch.setattr("ytp.podcasts_api.shutil.disk_usage", lambda path: client.disk(path))
    try:
        yield client
    finally:
        client.__exit__(None, None, None)


def test_the_probe_says_what_all_episodes_would_do(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(312, 60 * MB))
    body = api.post("/api/podcasts/probe", json={"feed": FEED}).json()
    assert len(body["episodes"]) == 50, "the list stays at 50"
    assert body["catalogue"] == {
        "episodes": 312,
        "to_download": 312,
        "skipped": 0,
        "size": 312 * 60 * MB,
        "sized": 312,
        "space_ok": True,
    }


def test_the_probe_counts_what_is_already_there(api, tmp_path):
    api.fetcher.feeds[FEED] = feed_of(*episodes(10))
    folder = tmp_path / "Podcasts" / "Test Cast"
    folder.mkdir()
    day = podcast_of(*episodes(10)).episodes[0].published.date().isoformat()
    (folder / f"{day} - Episode 0.mp3").write_bytes(b"x")
    catalogue = api.post("/api/podcasts/probe", json={"feed": FEED}).json()["catalogue"]
    assert (catalogue["to_download"], catalogue["skipped"]) == (9, 1)


def test_the_probe_warns_when_the_known_sizes_do_not_fit(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(100, 1 * GB))
    api.disk.free, api.disk.total = 120 * GB, 1000 * GB
    assert (
        api.post("/api/podcasts/probe", json={"feed": FEED}).json()["catalogue"]["space_ok"]
        is False
    )


def test_starting_a_run_queues_the_first_episodes_at_once(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(30))
    response = api.post("/api/podcasts/backlog", json={"feed": FEED})
    assert response.status_code == 201
    body = response.json()
    assert body["started"] is True
    assert body["backlog"]["total"] == 30
    assert body["backlog"]["folder"] == "Test Cast"
    assert len(jobs.list_jobs(api.app.state.db)) == 2
    assert "enclosure" not in response.text


def test_the_folder_name_can_be_given_and_a_subscription_decides_otherwise(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(3))
    podcast_subscriptions_add(api, "Subscribed Name")
    api.post("/api/podcasts/backlog", json={"feed": FEED, "name": "Other"})
    (run,) = podcast_backlog.visible(api.app.state.db)
    assert run.folder == "Subscribed Name"


def podcast_subscriptions_add(api, title):
    from ytp import podcast_subscriptions

    podcast_subscriptions.add(api.app.state.db, FEED, title)


def test_it_works_without_a_subscription_and_creates_none(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(3))
    api.post("/api/podcasts/backlog", json={"feed": FEED})
    assert api.get("/api/podcasts").json()["podcasts"] == []


def test_nothing_to_do_is_said_and_stores_no_run(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(2))
    api.post("/api/podcasts/backlog", json={"feed": FEED})
    finish_all(api.app.state.db)
    with api.app.state.db.transaction() as conn:
        conn.execute("UPDATE podcast_backlogs SET state = 'done'")
    # both episodes are queued/done now: asking again has nothing left to take
    finish_all(api.app.state.db)
    response = api.post("/api/podcasts/backlog", json={"feed": FEED})
    assert response.json()["started"] is False
    assert response.json()["total"] == 0


def test_a_run_that_cannot_fit_is_refused_before_anything_is_queued(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(100, 1 * GB))
    api.disk.free, api.disk.total = 120 * GB, 1000 * GB
    response = api.post("/api/podcasts/backlog", json={"feed": FEED})
    assert response.status_code == 409
    assert response.json()["kind"] == "not_enough_space"
    assert jobs.list_jobs(api.app.state.db) == []
    assert podcast_backlog.visible(api.app.state.db) == []


def test_asking_again_while_a_run_goes_joins_it(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(10))
    first = api.post("/api/podcasts/backlog", json={"feed": FEED}).json()
    again = api.post("/api/podcasts/backlog", json={"feed": FEED})
    assert again.status_code == 200
    assert again.json()["joined"] is True
    assert again.json()["backlog"]["id"] == first["backlog"]["id"]
    assert len(podcast_backlog.visible(api.app.state.db)) == 1


@pytest.mark.parametrize("body", [{}, {"feed": "not a link"}, {"feed": FEED, "extra": 1}])
def test_bad_requests_are_422(api, body):
    assert api.post("/api/podcasts/backlog", json=body).status_code == 422


def test_an_unreadable_feed_answers_like_the_other_podcast_routes(api):
    assert api.post("/api/podcasts/backlog", json={"feed": FEED}).status_code in (404, 502)


def test_the_list_and_the_dashboard_show_the_progress(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(10))
    api.post("/api/podcasts/backlog", json={"feed": FEED})
    finish_all(api.app.state.db)
    api.app.state.backlog_feeder.tick()
    listed = api.get("/api/podcasts/backlog").json()["backlogs"]
    assert [(r["total"], r["done"], r["state"]) for r in listed] == [(10, 2, "running")]
    dash = api.get("/api/dashboard").json()["backlogs"]
    assert dash == listed


def test_cancelling_through_the_api(api):
    api.fetcher.feeds[FEED] = feed_of(*episodes(10))
    run = api.post("/api/podcasts/backlog", json={"feed": FEED}).json()["backlog"]
    assert api.delete(f"/api/podcasts/backlog/{run['id']}").status_code == 204
    assert api.get("/api/podcasts/backlog").json()["backlogs"][0]["state"] == "cancelled"
    assert api.delete(f"/api/podcasts/backlog/{run['id']}").status_code == 404
    assert api.delete("/api/podcasts/backlog/9999").status_code == 404
