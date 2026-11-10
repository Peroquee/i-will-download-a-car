# ruff: noqa: E501
"""Resolution per subscription and per user, with a resolution right (#65)."""

from __future__ import annotations

import contextlib
import json
import random
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from tests.helpers.accounts_app import ADMIN, add_account, client_for, running
from tests.unit.test_feeds import CID, entry, feed
from ytp import accounts, resolution, subscriptions
from ytp import db as dbmod
from ytp.channels import Channel
from ytp.db import Database
from ytp.feeds import FeedClient
from ytp.subscription_poller import SubscriptionPoller

pytestmark = [pytest.mark.security, pytest.mark.real_auth]

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SUBSCRIBED = "2026-10-20T00:00:00.000Z"


# --- the migration ------------------------------------------------------------------------------------


def test_the_migration_gives_existing_accounts_the_right_and_adds_the_columns(tmp_path):
    conn = dbmod.connect(tmp_path / "app.db")
    migrations = dbmod.load_migrations()
    dbmod.migrate(conn, migrations[:14])
    conn.execute(
        "INSERT INTO accounts (username, password_hash, role) VALUES ('old', 'scrypt:x', 'user')"
    )
    assert dbmod.migrate(conn, migrations) == [
        "0015_resolution_rights.sql",
        "0016_job_note.sql",
    ]
    row = conn.execute(
        "SELECT default_max_height, can_set_resolution FROM accounts WHERE username = 'old'"
    ).fetchone()
    assert (row["default_max_height"], row["can_set_resolution"]) == (None, 1)
    assert "max_height" in [r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)")]
    # The values are the fixed list; the database refuses anything else.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE accounts SET default_max_height = 999")
    conn.execute(
        "INSERT INTO subscriptions (channel_id, channel_name) VALUES ('UC" + "a" * 22 + "', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE subscriptions SET max_height = 1")
    conn.close()


# --- the rule -----------------------------------------------------------------------------------------


def test_a_manual_download_uses_the_choice_the_users_default_or_the_server_default():
    choose = dict(allowed=True, default_max_height=720, server_default=1080)
    assert resolution.for_download(chosen=480, **choose) == 480  # the choice, when allowed
    assert resolution.for_download(chosen=None, **choose) == 720  # else the user's default
    assert (
        resolution.for_download(
            allowed=True, default_max_height=None, chosen=None, server_default=1080
        )
        is None  # else the server default (stored as "not set")
    )


def test_a_user_without_the_right_may_only_send_the_value_that_already_applies():
    assert (
        resolution.for_download(
            allowed=False, default_max_height=720, chosen=720, server_default=1080
        )
        == 720
    )
    assert (
        resolution.for_download(
            allowed=False, default_max_height=None, chosen=1080, server_default=1080
        )
        is None
    )
    with pytest.raises(resolution.NotAllowed):
        resolution.for_download(
            allowed=False, default_max_height=720, chosen=480, server_default=1080
        )


def test_the_choices_are_exactly_the_list_of_issue_47():
    assert resolution.MAX_HEIGHTS == (0, 2160, 1440, 1080, 720, 480, 360)
    assert resolution.valid(0)
    assert resolution.valid(2160)
    assert resolution.valid(360)
    for bad in (True, False, 999, 1, "720", 1080.5, [720], None):
        assert not resolution.valid(bad), bad


# --- the API ------------------------------------------------------------------------------------------


class FakeChannels:
    """The channel lookup without YouTube: one channel for every address."""

    async def resolve(self, ref):
        return Channel(CID, "Blender")


@contextlib.contextmanager
def app(tmp_path) -> Iterator[TestClient]:
    with running(tmp_path) as client:
        client.app.state.channels = FakeChannels()
        yield client


@pytest.fixture
def app_client(tmp_path):
    with app(tmp_path) as client:
        yield client


def make_user(client, name, *, can_choose=True, default_max_height=None):
    user = add_account(client, name)
    accounts.set_resolution_settings(
        client.app.state.db,
        user.id,
        can_set_resolution=can_choose,
        default_max_height=default_max_height,
    )
    return accounts.find(client.app.state.db, name)[0]


def post_download(client, video_id, **extra):
    return client.post(
        "/api/youtube/download",
        json={
            "url": f"https://youtu.be/{video_id}",
            "channel": "Blender",
            "title": "A video",
            **extra,
        },
    )


def metadata_of(client, video_id):
    rows = client.app.state.db.query("SELECT metadata FROM jobs WHERE external_id = ?", (video_id,))
    assert rows, "the job was not stored"
    return json.loads(rows[0]["metadata"])


def test_a_download_stores_the_users_default_when_it_sends_no_choice(app_client):
    alice = make_user(app_client, "alice", default_max_height=720)
    response = post_download(client_for(app_client, alice), "aaaaaaaaaaa")
    assert response.status_code == 201
    assert metadata_of(app_client, "aaaaaaaaaaa")["max_height"] == 720


def test_a_download_without_a_personal_default_keeps_the_server_default(app_client):
    alice = make_user(app_client, "alice")
    response = post_download(client_for(app_client, alice), "aaaaaaaaaaa")
    assert response.status_code == 201
    assert "max_height" not in metadata_of(app_client, "aaaaaaaaaaa")


def test_a_user_with_the_right_stores_the_chosen_resolution(app_client):
    alice = make_user(app_client, "alice", default_max_height=720)
    response = post_download(client_for(app_client, alice), "aaaaaaaaaaa", max_height=480)
    assert response.status_code == 201
    assert metadata_of(app_client, "aaaaaaaaaaa")["max_height"] == 480


def test_a_user_without_the_right_cannot_choose_another_resolution(app_client):
    alice = make_user(app_client, "alice", can_choose=False, default_max_height=720)
    browser = client_for(app_client, alice)
    assert post_download(browser, "aaaaaaaaaaa", max_height=720).status_code == 201
    refused = post_download(browser, "bbbbbbbbbbb", max_height=480)
    assert refused.status_code == 403
    assert "may not choose" in refused.json()["detail"]
    assert post_download(browser, "ccccccccccc").status_code == 201
    # with the right the same choice works
    accounts.set_resolution_settings(app_client.app.state.db, alice.id, can_set_resolution=True)
    assert post_download(browser, "ddddddddddd", max_height=480).status_code == 201


def test_an_admin_may_always_choose(app_client):
    boss = accounts.find(app_client.app.state.db, ADMIN)[0]
    accounts.set_resolution_settings(app_client.app.state.db, boss.id, can_set_resolution=False)
    response = post_download(client_for(app_client, ADMIN), "aaaaaaaaaaa", max_height=2160)
    assert response.status_code == 201
    assert metadata_of(app_client, "aaaaaaaaaaa")["max_height"] == 2160


def test_a_bad_value_is_a_400_even_with_the_right(app_client):
    alice = make_user(app_client, "alice")
    response = post_download(client_for(app_client, alice), "aaaaaaaaaaa", max_height=999)
    assert response.status_code == 400
    assert "max_height" in response.json()["detail"]
    assert app_client.app.state.db.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 0


def test_the_options_show_the_effective_default_and_the_right(app_client):
    alice = make_user(app_client, "alice", default_max_height=720)
    browser = client_for(app_client, alice)
    body = browser.get("/api/youtube/options").json()
    assert body == {
        "max_height": 720,
        "choices": [0, 2160, 1440, 1080, 720, 480, 360],
        "can_choose": True,
    }
    accounts.set_resolution_settings(app_client.app.state.db, alice.id, can_set_resolution=False)
    assert browser.get("/api/youtube/options").json()["can_choose"] is False
    # no personal default: the server default (1080 here)
    bob = make_user(app_client, "bob")
    assert client_for(app_client, bob).get("/api/youtube/options").json()["max_height"] == 1080


def test_an_admin_can_choose_even_with_the_right_switched_off(app_client):
    boss = accounts.find(app_client.app.state.db, ADMIN)[0]
    accounts.set_resolution_settings(app_client.app.state.db, boss.id, can_set_resolution=False)
    body = client_for(app_client, ADMIN).get("/api/youtube/options").json()
    assert body["can_choose"] is True


# --- the own settings ---------------------------------------------------------------------------------


def test_the_own_default_needs_the_right(app_client):
    alice = make_user(app_client, "alice", can_choose=False)
    browser = client_for(app_client, alice)
    refused = browser.patch("/api/me/settings", json={"default_max_height": 720})
    assert refused.status_code == 403
    assert accounts.get(app_client.app.state.db, alice.id).default_max_height is None
    accounts.set_resolution_settings(app_client.app.state.db, alice.id, can_set_resolution=True)
    saved = browser.patch("/api/me/settings", json={"default_max_height": 720})
    assert saved.status_code == 200
    assert saved.json()["account"]["default_max_height"] == 720
    cleared = browser.patch("/api/me/settings", json={"default_max_height": None})
    assert cleared.json()["account"]["default_max_height"] is None


def test_an_admin_sets_its_own_default_too(app_client):
    response = client_for(app_client, ADMIN).patch(
        "/api/me/settings", json={"default_max_height": 480}
    )
    assert response.status_code == 200
    assert response.json()["account"]["default_max_height"] == 480


@pytest.mark.parametrize("value", [999, 1, True, "720", 1080.5])
def test_a_bad_own_default_is_a_400(app_client, value):
    response = client_for(app_client, ADMIN).patch(
        "/api/me/settings", json={"default_max_height": value}
    )
    assert response.status_code == 400
    assert "max_height" in response.json()["detail"]


def test_a_non_admin_cannot_change_any_can_set_resolution(app_client):
    alice = add_account(app_client, "alice")
    bob = add_account(app_client, "bob")
    browser = client_for(app_client, alice)
    for account_id in (alice.id, bob.id):
        response = browser.patch(f"/api/accounts/{account_id}", json={"can_set_resolution": False})
        assert response.status_code == 403
    assert browser.patch("/api/me/settings", json={"can_set_resolution": False}).status_code == 403
    assert accounts.get(app_client.app.state.db, alice.id).can_set_resolution is True


def test_an_admin_sets_a_users_default_and_right(app_client):
    alice = add_account(app_client, "alice")
    admin = client_for(app_client, ADMIN)
    response = admin.patch(
        f"/api/accounts/{alice.id}",
        json={"default_max_height": 720, "can_set_resolution": False},
    )
    assert response.status_code == 200
    assert response.json()["account"]["default_max_height"] == 720
    assert response.json()["account"]["can_set_resolution"] is False
    listed = {a["username"]: a for a in admin.get("/api/accounts").json()["accounts"]}
    assert listed["alice"]["default_max_height"] == 720
    assert listed["alice"]["can_set_resolution"] is False
    cleared = admin.patch(f"/api/accounts/{alice.id}", json={"default_max_height": None})
    assert cleared.json()["account"]["default_max_height"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"default_max_height": 999},
        {"default_max_height": True},
        {"can_set_resolution": "yes"},
    ],
)
def test_a_bad_account_update_is_a_400(app_client, body):
    alice = add_account(app_client, "alice")
    response = client_for(app_client, ADMIN).patch(f"/api/accounts/{alice.id}", json=body)
    assert response.status_code == 400


# --- subscriptions ------------------------------------------------------------------------------------


def test_a_subscription_with_a_resolution_is_created(app_client):
    alice = make_user(app_client, "alice")
    response = client_for(app_client, alice).post(
        "/api/subscriptions", json={"channel": "@kurzgesagt", "max_height": 720}
    )
    assert response.status_code == 201
    assert response.json()["max_height"] == 720
    listed = client_for(app_client, alice).get("/api/subscriptions").json()["subscriptions"]
    assert listed[0]["max_height"] == 720


def test_a_subscription_resolution_needs_the_right(app_client):
    alice = make_user(app_client, "alice", can_choose=False)
    browser = client_for(app_client, alice)
    refused = browser.post("/api/subscriptions", json={"channel": "@kurzgesagt", "max_height": 720})
    assert refused.status_code == 403
    created = browser.post("/api/subscriptions", json={"channel": "@kurzgesagt"})
    assert created.status_code == 201
    assert created.json()["max_height"] is None
    sub_id = created.json()["id"]
    refused = browser.patch(f"/api/subscriptions/{sub_id}", json={"max_height": 720})
    assert refused.status_code == 403
    assert subscriptions.get(app_client.app.state.db, sub_id).max_height is None
    # with the right the update works; the list shows it
    accounts.set_resolution_settings(app_client.app.state.db, alice.id, can_set_resolution=True)
    saved = browser.patch(f"/api/subscriptions/{sub_id}", json={"max_height": 720})
    assert saved.status_code == 200
    assert saved.json()["max_height"] == 720
    listed = browser.get("/api/subscriptions").json()["subscriptions"]
    assert listed[0]["max_height"] == 720
    cleared = browser.patch(f"/api/subscriptions/{sub_id}", json={"max_height": None})
    assert cleared.json()["max_height"] is None


def test_an_admin_may_always_set_the_subscription_resolution(app_client):
    alice = make_user(app_client, "alice")
    sub_id = (
        client_for(app_client, alice)
        .post("/api/subscriptions", json={"channel": "@kurzgesagt"})
        .json()["id"]
    )
    boss = accounts.find(app_client.app.state.db, ADMIN)[0]
    accounts.set_resolution_settings(app_client.app.state.db, boss.id, can_set_resolution=False)
    response = client_for(app_client, ADMIN).patch(
        f"/api/subscriptions/{sub_id}", json={"max_height": 2160}
    )
    assert response.status_code == 200
    assert response.json()["max_height"] == 2160


@pytest.mark.parametrize("value", [999, 1, True, "720"])
def test_a_bad_subscription_resolution_is_a_400(app_client, value):
    alice = make_user(app_client, "alice")
    assert (
        client_for(app_client, alice)
        .post("/api/subscriptions", json={"channel": "@kurzgesagt", "max_height": value})
        .status_code
        == 400
    )
    sub_id = (
        client_for(app_client, alice)
        .post("/api/subscriptions", json={"channel": "@kurzgesagt"})
        .json()["id"]
    )
    response = client_for(app_client, alice).patch(
        f"/api/subscriptions/{sub_id}", json={"max_height": value}
    )
    assert response.status_code == 400


def test_a_non_follower_cannot_change_a_subscription(app_client):
    alice = make_user(app_client, "alice")
    bob = make_user(app_client, "bob")
    sub_id = (
        client_for(app_client, alice)
        .post("/api/subscriptions", json={"channel": "@kurzgesagt"})
        .json()["id"]
    )
    # bob may choose resolutions, but he does not follow the channel: 404, not 403
    refused = client_for(app_client, bob).patch(
        f"/api/subscriptions/{sub_id}", json={"max_height": 720}
    )
    assert refused.status_code == 404


# --- the poller ---------------------------------------------------------------------------------------


class FakeFeeds(FeedClient):
    def __init__(self):
        super().__init__()
        self.answers: dict[str, bytes] = {}

    def fetch(self, channel_id):
        return self.answers.get(channel_id, feed(channel=channel_id))


def poller_database(tmp_path) -> Database:
    database = Database.open(tmp_path / "app.db")
    s, _ = subscriptions.add(database, CID, "Blender")
    with database.transaction() as conn:
        conn.execute("UPDATE subscriptions SET created_at = ? WHERE id = ?", (SUBSCRIBED, s.id))
    return database


def queued_metadata(database) -> dict[str, dict]:
    return {
        row["external_id"]: json.loads(row["metadata"])
        for row in database.query("SELECT external_id, metadata FROM jobs")
    }


def test_the_poller_puts_the_subscription_resolution_into_each_job(tmp_path):
    database = poller_database(tmp_path)
    try:
        with database.transaction() as conn:
            conn.execute("UPDATE subscriptions SET max_height = 720")
        feeds = FakeFeeds()
        feeds.answers[CID] = feed(
            entry("aaaaaaaaaaa", "New one", "2026-11-01T10:00:00+00:00"), channel=CID
        )
        poller = SubscriptionPoller(
            database, feeds, None, poll_minutes=60, now=lambda: NOW, rng=random.Random(1)
        )
        assert poller.run_once().queued == 1
        assert queued_metadata(database)["aaaaaaaaaaa"]["max_height"] == 720
    finally:
        database.close()


def test_a_subscription_without_a_resolution_leaves_the_server_default(tmp_path):
    database = poller_database(tmp_path)
    try:
        feeds = FakeFeeds()
        feeds.answers[CID] = feed(
            entry("aaaaaaaaaaa", "New one", "2026-11-01T10:00:00+00:00"), channel=CID
        )
        poller = SubscriptionPoller(
            database, feeds, None, poll_minutes=60, now=lambda: NOW, rng=random.Random(1)
        )
        assert poller.run_once().queued == 1
        assert "max_height" not in queued_metadata(database)["aaaaaaaaaaa"]
    finally:
        database.close()
