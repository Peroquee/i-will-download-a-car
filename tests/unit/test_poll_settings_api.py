# ruff: noqa: E501
"""Check interval, check time and the server-wide defaults (issue #79): storage, API, rights and
the poller that honours them."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.accounts_app import ADMIN, ENV, add_account, client_for, running
from ytp import accounts, podcast_subscriptions, subscriptions
from ytp import db as dbmod
from ytp.config import Settings
from ytp.db import Database
from ytp.feeds import FeedClient
from ytp.podcast_jobs import PodcastPoller
from ytp.podcasts import PodcastFeedClient
from ytp.server_defaults import ServerDefaults
from ytp.subscription_poller import SubscriptionPoller
from ytp.youtube_download import YoutubeDownloader

pytestmark = [pytest.mark.real_auth, pytest.mark.security]

CID = "UCSMOQeBJ2RAnuFungnQOxLg"
FEED = "https://example.org/feed.xml"
BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def settings(tmp_path, **env) -> Settings:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    return Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config), **env})


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def database(tmp_path):
    db = Database.open(tmp_path / "app.db")
    yield db
    db.close()


@pytest.fixture
def defaults(tmp_path, database):
    return ServerDefaults(lambda: database, settings(tmp_path), zone=BERLIN)


# --- the migration ------------------------------------------------------------------------------------


def test_the_migration_adds_nullable_columns_and_refuses_bad_values(tmp_path):
    conn = dbmod.connect(tmp_path / "app.db")
    dbmod.migrate(conn)
    conn.execute("INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC1', 'One')")
    row = conn.execute("SELECT poll_minutes, poll_at FROM subscriptions").fetchone()
    assert (row["poll_minutes"], row["poll_at"]) == (None, None), "existing rows follow the server"
    for value in (0, 15, 1440):
        conn.execute("UPDATE subscriptions SET poll_minutes = ?", (value,))
    for value in (None, "", "00:00", "23:59"):
        conn.execute("UPDATE subscriptions SET poll_at = ?", (value,))
    for column, bad in [("poll_minutes", 14), ("poll_minutes", 1441), ("poll_minutes", -1),
                        ("poll_at", "24:00"), ("poll_at", "6:00"), ("poll_at", "06:60"), ("poll_at", "ab:cd")]:  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE subscriptions SET {column} = ?", (bad,))
    conn.execute("INSERT INTO podcast_subscriptions (feed_url, title) VALUES ('https://a/b', 'P')")
    conn.execute("UPDATE podcast_subscriptions SET poll_minutes = 30")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE podcast_subscriptions SET poll_minutes = 15")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE podcast_subscriptions SET poll_at = '99:99'")
    conn.close()


# --- the server-wide defaults ---------------------------------------------------------------------------


def test_before_an_admin_saves_anything_the_defaults_are_the_environments(tmp_path, database):
    from_env = ServerDefaults(
        lambda: database,
        settings(
            tmp_path,
            YOUTUBE_MAX_HEIGHT="720",
            SUBSCRIPTION_POLL_MINUTES="30",
            PODCAST_POLL_MINUTES="0",
        ),
        zone=BERLIN,
    )
    snap = from_env.snapshot()
    assert snap["max_height"] == 720
    assert (snap["subscription_poll_minutes"], snap["subscription_poll_at"]) == (30, None)
    assert (snap["podcast_poll_minutes"], snap["podcast_poll_at"]) == (0, None)
    assert not any(snap["custom"].values())
    assert snap["timezone"] == "Europe/Berlin"
    assert snap["limits"] == {
        "choices": [0, 2160, 1440, 1080, 720, 480, 360],
        "min_minutes": {"channel": 15, "podcast": 30},
        "max_minutes": 1440,
    }
    assert from_env.schedule("channel").minutes == 30
    assert from_env.max_height() == 720


def test_a_saved_default_wins_and_takes_effect_at_once(defaults):
    assert defaults.max_height() == 1080
    snap = defaults.update({"max_height": 480, "subscription_poll_minutes": 120})
    assert defaults.max_height() == 480
    assert snap["max_height"] == 480
    assert snap["subscription_poll_minutes"] == 120
    assert snap["custom"]["max_height"]
    assert snap["custom"]["subscription_poll_minutes"]
    assert not snap["custom"]["podcast_poll_minutes"]
    assert defaults.schedule("channel").minutes == 120
    assert defaults.schedule("podcast").minutes == 240, "the podcast default is separate"
    defaults.update({"podcast_poll_minutes": 0, "podcast_poll_at": "07:30"})
    assert defaults.schedule("podcast") == defaults.schedule("podcast").__class__(0, "07:30")


def test_none_goes_back_to_the_environment_and_an_empty_time_means_none(defaults):
    defaults.update(
        {"max_height": 0, "subscription_poll_minutes": 0, "subscription_poll_at": "06:00"}
    )
    assert defaults.max_height() == 0, "0 = best is a value, not 'unset'"
    assert defaults.schedule("channel").minutes == 0
    after = defaults.update(
        {"max_height": None, "subscription_poll_minutes": None, "subscription_poll_at": ""}
    )
    assert after["max_height"] == 1080
    assert after["subscription_poll_minutes"] == 60
    assert after["subscription_poll_at"] is None
    assert not any(after["custom"].values())


@pytest.mark.parametrize(
    "bad",
    [
        {"max_height": 999}, {"max_height": True}, {"max_height": "720"},
        {"subscription_poll_minutes": 14}, {"subscription_poll_minutes": 1441},
        {"subscription_poll_minutes": "60"}, {"subscription_poll_minutes": True},
        {"podcast_poll_minutes": 29}, {"podcast_poll_minutes": 15},
        {"subscription_poll_at": "24:00"}, {"podcast_poll_at": 600}, {"podcast_poll_at": "6:00"},
        {"nonsense": 1}, {},
    ],
)  # fmt: skip
def test_a_bad_default_is_refused_and_nothing_is_saved(defaults, bad):
    before = defaults.snapshot()
    with pytest.raises(Exception, match=r"."):
        defaults.update({"max_height": 480, **bad} if bad else bad)
    assert defaults.snapshot() == before, "all or nothing"


def test_a_stored_value_that_is_not_valid_any_more_is_ignored(defaults, database):
    with database.transaction() as conn:
        conn.executemany(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            [("default.max_height", "999"), ("default.channel_poll_minutes", "5"),
             ("default.channel_poll_at", "later"), ("default.podcast_poll_minutes", "x")],
        )  # fmt: skip
    snap = defaults.snapshot()
    assert (snap["max_height"], snap["subscription_poll_minutes"]) == (1080, 60)
    assert snap["subscription_poll_at"] is None
    assert snap["podcast_poll_minutes"] == 240


def test_without_a_database_the_defaults_are_the_environments(tmp_path):
    bare = ServerDefaults(lambda: None, settings(tmp_path), zone=BERLIN)
    assert bare.max_height() == 1080
    assert bare.schedule("channel").minutes == 60
    with pytest.raises(RuntimeError):
        bare.update({"max_height": 720})


def test_the_downloader_asks_for_the_default_at_each_download():
    box = {"height": 1080}
    downloader = YoutubeDownloader(max_height=1080, max_height_source=lambda: box["height"])
    assert downloader._default_height() == 1080
    box["height"] = 360
    assert downloader._default_height() == 360
    assert YoutubeDownloader(max_height=720)._default_height() == 720


# --- the poller -----------------------------------------------------------------------------------------


def channel(database, **values):
    sub, _ = subscriptions.add(database, CID, "Blender")
    columns = {"created_at": "2026-10-01T00:00:00.000Z", **values}
    with database.transaction() as conn:
        for column, value in columns.items():
            conn.execute(f"UPDATE subscriptions SET {column} = ? WHERE id = ?", (value, sub.id))
    return sub


def channel_poller(database, defaults, now=NOW):
    return SubscriptionPoller(
        database,
        FeedClient(),
        None,
        poll_minutes=60,
        defaults=defaults,
        zone=BERLIN,
        now=lambda: now,
    )


def test_a_subscription_with_its_own_interval_is_checked_only_when_it_is_due(database, defaults):
    channel(database, poll_minutes=240, last_checked=iso(NOW - timedelta(hours=3)))
    poller = channel_poller(database, defaults)
    assert poller.due() == [], "3 h ago is within its own 4 h, although the server says 1 h"
    channel_poller(database, defaults, NOW + timedelta(hours=3))
    assert len(channel_poller(database, defaults, NOW + timedelta(hours=3)).due()) == 1


def test_without_its_own_interval_it_follows_the_server_default_and_a_change_applies_at_once(
    database, defaults
):
    channel(database, last_checked=iso(NOW - timedelta(minutes=90)))
    poller = channel_poller(database, defaults)
    assert len(poller.due()) == 1, "the default is 60 minutes"
    defaults.update({"subscription_poll_minutes": 240})  # no restart, no new poller
    assert poller.due() == []
    defaults.update({"subscription_poll_minutes": None})
    assert len(poller.due()) == 1


def test_a_paused_subscription_is_never_due_and_a_paused_server_pauses_the_rest(database, defaults):
    channel(database, poll_minutes=0, last_checked=iso(NOW - timedelta(days=3)))
    assert channel_poller(database, defaults).due() == []
    with database.transaction() as conn:
        conn.execute("UPDATE subscriptions SET poll_minutes = NULL")
    assert len(channel_poller(database, defaults).due()) == 1
    defaults.update({"subscription_poll_minutes": 0})
    assert channel_poller(database, defaults).due() == []
    with database.transaction() as conn:
        conn.execute("UPDATE subscriptions SET poll_minutes = 60")
    assert len(channel_poller(database, defaults).due()) == 1, "its own value beats the server's"


def test_a_subscription_with_a_start_time_is_checked_at_the_clock_times(database, defaults):
    # every 6 h from 06:00 (Berlin, winter time): 06, 12, 18, 00. It is 13:00 there at NOW.
    channel(
        database,
        poll_minutes=360,
        poll_at="06:00",
        last_checked=iso(datetime(2026, 11, 10, 11, 0, tzinfo=UTC)),
    )
    assert channel_poller(database, defaults).due() == [], (
        "checked after 12:00 Berlin; next is 18:00"
    )
    evening = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)  # 18:00 in Berlin
    assert len(channel_poller(database, defaults, evening).due()) == 1


def test_a_subscriptions_own_start_time_beats_the_servers_and_empty_means_none(database, defaults):
    defaults.update({"subscription_poll_minutes": 360, "subscription_poll_at": "06:00"})
    last = iso(datetime(2026, 11, 10, 11, 0, tzinfo=UTC))
    sub = channel(database, last_checked=last)
    now = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)  # 15:00 Berlin
    assert channel_poller(database, defaults, now).due() == [], "server: 06/12/18/00, next 18:00"
    with database.transaction() as conn:
        conn.execute("UPDATE subscriptions SET poll_at = '14:30' WHERE id = ?", (sub.id,))
    # own anchor 14:30, 6 h: 14:30 Berlin = 13:30 UTC has passed since the check at 11:00 UTC
    assert len(channel_poller(database, defaults, now).due()) == 1
    with database.transaction() as conn:
        conn.execute("UPDATE subscriptions SET poll_at = '' WHERE id = ?", (sub.id,))
    # '' = no start time: back to "interval since the last check" (3 h < 4.8 h)
    assert channel_poller(database, defaults, now).due() == []


def test_a_failed_channel_is_retried_soon_even_with_a_start_time(database, defaults):
    last = NOW - timedelta(minutes=11)
    channel(database, poll_minutes=1440, poll_at="06:00", last_checked=iso(last), last_error="down")
    assert len(channel_poller(database, defaults).due()) == 1


def test_a_podcast_follows_the_podcast_default_and_its_own_values(database, defaults):
    sub, _ = podcast_subscriptions.add(database, FEED, "Cast")
    last = iso(NOW - timedelta(hours=3))
    with database.transaction() as conn:
        conn.execute("UPDATE podcast_subscriptions SET last_checked = ?", (last,))
    poller = PodcastPoller(
        database,
        PodcastFeedClient(None),
        None,
        poll_minutes=240,
        defaults=defaults,
        zone=BERLIN,
        now=lambda: NOW,
    )  # type: ignore[arg-type]
    assert poller.due() == [], "the podcast default is 240 minutes, not the channels' 60"
    defaults.update({"subscription_poll_minutes": 15})
    assert poller.due() == [], "the channel default does not touch podcasts"
    podcast_subscriptions.update_settings(database, sub.id, {"poll_minutes": 30})
    assert len(poller.due()) == 1
    podcast_subscriptions.update_settings(database, sub.id, {"poll_minutes": 0})
    assert poller.due() == []


# --- the API -----------------------------------------------------------------------------------------------


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TZ", "Europe/Berlin"
    )  # the container sets it (compose); a CI runner may not
    with running(tmp_path) as client:
        yield client


def make_user(client, name, *, can_choose=True):
    user = add_account(client, name)
    accounts.set_resolution_settings(client.app.state.db, user.id, can_set_resolution=can_choose)
    return accounts.find(client.app.state.db, name)[0]


def follow_channel(client, account) -> int:
    sub, _ = subscriptions.add(client.app.state.db, CID, "Blender")
    accounts.follow(client.app.state.db, "channel", sub.id, account.id)
    return sub.id


def follow_podcast(client, account) -> int:
    sub, _ = podcast_subscriptions.add(client.app.state.db, FEED, "Cast")
    accounts.follow(client.app.state.db, "podcast", sub.id, account.id)
    return sub.id


def test_everyone_signed_in_reads_the_defaults_but_a_stranger_does_not(app_client):
    alice = make_user(app_client, "alice")
    body = client_for(app_client, alice).get("/api/settings/defaults").json()
    assert body["max_height"] == 1080
    assert body["subscription_poll_minutes"] == 60
    assert body["podcast_poll_minutes"] == 240
    assert body["timezone"] == "Europe/Berlin"
    assert body["custom"]["max_height"] is False
    assert client_for(app_client, None).get("/api/settings/defaults").status_code == 401


def test_only_an_admin_changes_the_defaults(app_client):
    alice = make_user(app_client, "alice")
    mine = client_for(app_client, alice)
    assert mine.patch("/api/settings/defaults", json={"max_height": 720}).status_code == 403
    assert (
        client_for(app_client, None)
        .patch("/api/settings/defaults", json={"max_height": 720})
        .status_code
        == 401
    )
    assert app_client.app.state.defaults.max_height() == 1080
    boss = client_for(app_client, ADMIN)
    saved = boss.patch(
        "/api/settings/defaults",
        json={"max_height": 720, "podcast_poll_minutes": 360, "podcast_poll_at": "05:00"},
    )
    assert saved.status_code == 200
    assert saved.json()["max_height"] == 720
    assert saved.json()["custom"]["podcast_poll_at"] is True
    seen = mine.get("/api/settings/defaults").json()
    assert (seen["max_height"], seen["podcast_poll_minutes"], seen["podcast_poll_at"]) == (
        720,
        360,
        "05:00",
    )


@pytest.mark.parametrize(
    "body",
    [{"max_height": 999}, {"subscription_poll_minutes": 5}, {"podcast_poll_minutes": 20},
     {"subscription_poll_at": "25:00"}, {"nothing": 1}, {}],
)  # fmt: skip
def test_a_bad_default_is_a_400_and_changes_nothing(app_client, body):
    boss = client_for(app_client, ADMIN)
    assert boss.patch("/api/settings/defaults", json=body).status_code == 400
    assert not any(boss.get("/api/settings/defaults").json()["custom"].values())


def test_a_defaults_body_that_is_not_an_object_is_a_422(app_client):
    boss = client_for(app_client, ADMIN)
    assert boss.patch("/api/settings/defaults", content=b"nope").status_code == 422
    assert boss.patch("/api/settings/defaults", json=[1]).status_code == 422


def test_the_admins_default_resolution_is_what_a_download_and_the_options_use(app_client):
    alice = make_user(app_client, "alice")
    mine = client_for(app_client, alice)
    assert mine.get("/api/youtube/options").json()["max_height"] == 1080
    client_for(app_client, ADMIN).patch("/api/settings/defaults", json={"max_height": 480})
    assert mine.get("/api/youtube/options").json()["max_height"] == 480


def test_the_channel_interval_and_time_are_saved_cleared_and_listed(app_client):
    alice = make_user(app_client, "alice")
    sub_id = follow_channel(app_client, alice)
    mine = client_for(app_client, alice)
    saved = mine.patch(
        f"/api/subscriptions/{sub_id}", json={"poll_minutes": 120, "poll_at": "06:00"}
    )
    assert saved.status_code == 200
    assert (saved.json()["poll_minutes"], saved.json()["poll_at"]) == (120, "06:00")
    listed = mine.get("/api/subscriptions").json()["subscriptions"][0]
    assert (listed["poll_minutes"], listed["poll_at"], listed["max_height"]) == (120, "06:00", None)
    assert (
        mine.patch(f"/api/subscriptions/{sub_id}", json={"poll_minutes": 0}).json()["poll_minutes"]
        == 0
    )
    assert mine.patch(f"/api/subscriptions/{sub_id}", json={"poll_at": ""}).json()["poll_at"] == ""
    both = mine.patch(
        f"/api/subscriptions/{sub_id}",
        json={"poll_minutes": None, "poll_at": None, "max_height": 720},
    )
    assert (both.json()["poll_minutes"], both.json()["poll_at"], both.json()["max_height"]) == (
        None,
        None,
        720,
    )
    # only what was sent changes
    mine.patch(f"/api/subscriptions/{sub_id}", json={"poll_minutes": 60})
    assert subscriptions.get(app_client.app.state.db, sub_id).max_height == 720


@pytest.mark.parametrize(
    "body",
    [{"poll_minutes": 14}, {"poll_minutes": 1441}, {"poll_minutes": -5}, {"poll_minutes": "60"},
     {"poll_minutes": True}, {"poll_minutes": 60.5}, {"poll_at": "24:00"}, {"poll_at": "6:00"},
     {"poll_at": 600}, {"poll_at": "06:00:00"}],
)  # fmt: skip
def test_a_bad_channel_interval_or_time_is_a_400_and_nothing_changes(app_client, body):
    alice = make_user(app_client, "alice")
    sub_id = follow_channel(app_client, alice)
    response = client_for(app_client, alice).patch(f"/api/subscriptions/{sub_id}", json=body)
    assert response.status_code == 400
    assert "detail" in response.json()
    sub = subscriptions.get(app_client.app.state.db, sub_id)
    assert (sub.poll_minutes, sub.poll_at) == (None, None)


@pytest.mark.parametrize("body", [{}, {"poll_minutes": 60, "enabled": False}, {"name": "x"}, [1]])
def test_an_unknown_or_empty_channel_update_is_a_422(app_client, body):
    alice = make_user(app_client, "alice")
    sub_id = follow_channel(app_client, alice)
    assert (
        client_for(app_client, alice).patch(f"/api/subscriptions/{sub_id}", json=body).status_code
        == 422
    )


def test_the_channel_interval_needs_the_right_and_following(app_client):
    alice = make_user(app_client, "alice", can_choose=False)
    bob = make_user(app_client, "bob")
    sub_id = follow_channel(app_client, alice)
    refused = client_for(app_client, alice).patch(
        f"/api/subscriptions/{sub_id}", json={"poll_minutes": 120}
    )
    assert refused.status_code == 403
    assert "settings" in refused.json()["detail"]
    assert (
        client_for(app_client, bob)
        .patch(f"/api/subscriptions/{sub_id}", json={"poll_minutes": 120})
        .status_code
        == 404
    ), "bob does not follow it"
    assert (
        client_for(app_client, None)
        .patch(f"/api/subscriptions/{sub_id}", json={"poll_minutes": 120})
        .status_code
        == 401
    )
    assert subscriptions.get(app_client.app.state.db, sub_id).poll_minutes is None
    accounts.set_resolution_settings(app_client.app.state.db, alice.id, can_set_resolution=True)
    assert (
        client_for(app_client, alice)
        .patch(f"/api/subscriptions/{sub_id}", json={"poll_minutes": 120})
        .status_code
        == 200
    )
    # an admin may always, followed or not, even with the right switched off
    boss = accounts.find(app_client.app.state.db, ADMIN)[0]
    accounts.set_resolution_settings(app_client.app.state.db, boss.id, can_set_resolution=False)
    assert (
        client_for(app_client, ADMIN)
        .patch(f"/api/subscriptions/{sub_id}", json={"poll_minutes": 0})
        .status_code
        == 200
    )


def test_a_resolution_refusal_keeps_its_own_message(app_client):
    alice = make_user(app_client, "alice", can_choose=False)
    sub_id = follow_channel(app_client, alice)
    refused = client_for(app_client, alice).patch(
        f"/api/subscriptions/{sub_id}", json={"max_height": 720}
    )
    assert refused.status_code == 403
    assert "resolution" in refused.json()["detail"]


def test_the_podcast_interval_and_time_are_saved_cleared_and_listed(app_client):
    alice = make_user(app_client, "alice")
    sub_id = follow_podcast(app_client, alice)
    mine = client_for(app_client, alice)
    saved = mine.patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": 30, "poll_at": "18:00"})
    assert saved.status_code == 200
    assert (saved.json()["poll_minutes"], saved.json()["poll_at"]) == (30, "18:00")
    listed = mine.get("/api/podcasts").json()["podcasts"][0]
    assert (listed["poll_minutes"], listed["poll_at"]) == (30, "18:00")
    assert (
        mine.patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": 0}).json()["poll_minutes"] == 0
    )
    cleared = mine.patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": None, "poll_at": None})
    assert (cleared.json()["poll_minutes"], cleared.json()["poll_at"]) == (None, None)


@pytest.mark.parametrize(
    "body",
    [{"poll_minutes": 15}, {"poll_minutes": 29}, {"poll_minutes": 1441}, {"poll_at": "7:00"}],
)
def test_a_bad_podcast_interval_is_a_400_and_podcasts_have_a_30_minute_minimum(app_client, body):
    alice = make_user(app_client, "alice")
    sub_id = follow_podcast(app_client, alice)
    assert (
        client_for(app_client, alice).patch(f"/api/podcasts/{sub_id}", json=body).status_code == 400
    )
    assert (
        "between 30 and 1440"
        in client_for(app_client, alice)
        .patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": 15})
        .json()["detail"]
    )


@pytest.mark.parametrize("body", [{}, {"max_height": 720}, {"poll_minutes": 60, "title": "x"}])
def test_an_unknown_or_empty_podcast_update_is_a_422(app_client, body):
    alice = make_user(app_client, "alice")
    sub_id = follow_podcast(app_client, alice)
    assert (
        client_for(app_client, alice).patch(f"/api/podcasts/{sub_id}", json=body).status_code == 422
    )


def test_the_podcast_interval_needs_the_right_and_following(app_client):
    alice = make_user(app_client, "alice", can_choose=False)
    bob = make_user(app_client, "bob")
    sub_id = follow_podcast(app_client, alice)
    assert (
        client_for(app_client, alice)
        .patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": 60})
        .status_code
        == 403
    )
    assert (
        client_for(app_client, bob)
        .patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": 60})
        .status_code
        == 404
    )
    assert (
        client_for(app_client, bob)
        .patch("/api/podcasts/9999", json={"poll_minutes": 60})
        .status_code
        == 404
    )
    assert (
        client_for(app_client, None)
        .patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": 60})
        .status_code
        == 401
    )
    assert podcast_subscriptions.get(app_client.app.state.db, sub_id).poll_minutes is None
    assert (
        client_for(app_client, ADMIN)
        .patch(f"/api/podcasts/{sub_id}", json={"poll_minutes": 60})
        .status_code
        == 200
    )


def test_a_channel_cannot_be_changed_through_the_podcast_route_or_the_other_way_round(app_client):
    alice = make_user(app_client, "alice")
    channel_id = follow_channel(app_client, alice)
    podcast_id = follow_podcast(app_client, alice)
    assert channel_id == podcast_id == 1, "the ids overlap: the route decides the table"
    client_for(app_client, alice).patch(f"/api/podcasts/{podcast_id}", json={"poll_minutes": 90})
    assert subscriptions.get(app_client.app.state.db, channel_id).poll_minutes is None
    assert podcast_subscriptions.get(app_client.app.state.db, podcast_id).poll_minutes == 90
