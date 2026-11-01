# ruff: noqa: E501
"""Channel subscriptions (issue #19): address parsing, the channel lookup, the table, the API."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ytp import subscriptions
from ytp.channels import (
    ChannelRef,
    ChannelResolver,
    InvalidChannel,
    parse_channel_ref,
)
from ytp.config import Settings
from ytp.db import Database
from ytp.main import create_app
from ytp.youtube import ProbeError, YoutubeProbe

CID = "UCsXVk37bltHxD1rDPwtNM8Q"
CID2 = "UCX6OQ3DkcsbYNE6H8uQQuVA"
VIDEO = "dQw4w9WgXcQ"
ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


# --- the address ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        (CID, "id", CID),
        (f"  {CID}\n", "id", CID),
        ("@kurzgesagt", "handle", "@kurzgesagt"),
        ("@Some.Name_1-x", "handle", "@Some.Name_1-x"),
        (f"https://www.youtube.com/channel/{CID}", "id", CID),
        (f"https://youtube.com/channel/{CID}/videos", "id", CID),
        (f"https://m.youtube.com/channel/{CID}/about?foo=bar#x", "id", CID),
        ("https://www.youtube.com/@kurzgesagt", "handle", "@kurzgesagt"),
        ("https://www.youtube.com/@kurzgesagt/videos", "handle", "@kurzgesagt"),
        (f"https://www.youtube.com/watch?v={VIDEO}", "video", VIDEO),
        (f"https://youtu.be/{VIDEO}", "video", VIDEO),
        (f"https://www.youtube.com/watch?v={VIDEO}&list=PLx&t=5", "video", VIDEO),
    ],
)
def test_accepted_channel_addresses(text, kind, value):
    assert parse_channel_ref(text) == ChannelRef(kind, value)


@pytest.mark.security
@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "kurzgesagt",
        "@ab",
        "@" + "a" * 31,
        "@bad name",
        "UCshort",
        CID + "x",
        "https://example.com/@kurzgesagt",
        f"https://youtube.com.evil.example/channel/{CID}",
        f"https://evil.example/https://www.youtube.com/channel/{CID}",
        f"ftp://www.youtube.com/channel/{CID}",
        f"https://user:pw@www.youtube.com/channel/{CID}",
        f"https://www.youtube.com:8443/channel/{CID}",
        "https://www.youtube.com/c/SomeName",
        "https://www.youtube.com/user/SomeName",
        "https://www.youtube.com/playlist?list=PLabc",
        f"https://www.youtube.com/channel/{CID}/../../etc",
        f"https://www.youtube.com/channel/{CID}/videos/extra",
        "https://www.youtube.com/@kurzgesagt/evil",
        "https://www.youtube.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "x" * 400,
        f"@kurzgesagt\n{CID}",
        None,
        5,
    ],
)
def test_refused_channel_addresses(text):
    with pytest.raises(InvalidChannel):
        parse_channel_ref(text)


def test_what_yt_dlp_gets_is_rebuilt_from_the_parsed_value():
    assert ChannelRef("id", CID).url == f"https://www.youtube.com/channel/{CID}"
    assert ChannelRef("handle", "@kurz").url == "https://www.youtube.com/@kurz"
    assert ChannelRef("video", VIDEO).url == f"https://www.youtube.com/watch?v={VIDEO}"


# --- the lookup ----------------------------------------------------------------------------------------


def channel_info(channel_id=CID, name="Kurzgesagt - In a Nutshell"):
    return {
        "_type": "playlist",
        "channel_id": channel_id,
        "channel": name,
        "title": name + " - Videos",
    }


def fake_extract(info=None, error=None, seen=None):
    def extract(url, options):
        if seen is not None:
            seen.append((url, options))
        if error is not None:
            raise error
        return dict(channel_info() if info is None else info)

    return extract


def resolver(extract, **kwargs):
    probe = YoutubeProbe(proxy="http://gluetun:8888", extract=extract)
    return ChannelResolver(probe, extract=extract, **kwargs)


def resolve(res, ref):
    return asyncio.run(res.resolve(ref))


def test_a_handle_is_resolved_through_the_proxy_with_restricted_options():
    seen = []
    channel = resolve(resolver(fake_extract(seen=seen)), ChannelRef("handle", "@kurzgesagt"))
    assert (channel.channel_id, channel.name) == (CID, "Kurzgesagt - In a Nutshell")
    url, options = seen[0]
    assert url == "https://www.youtube.com/@kurzgesagt"
    assert options["proxy"] == "http://gluetun:8888"
    assert options["allowed_extractors"] == ["youtube", "youtube:tab"]  # YouTube only
    assert options["skip_download"] is True
    assert options["playlistend"] == 1
    assert options["cachedir"] is False


def test_an_id_must_come_back_as_the_same_id():
    res = resolver(fake_extract(info=channel_info(CID2)))
    with pytest.raises(ProbeError) as exc:
        resolve(res, ChannelRef("id", CID))
    assert exc.value.kind == "unknown"


@pytest.mark.parametrize("bad", [None, "", "not-a-channel-id", "UC123", 7])
def test_an_unusable_channel_id_in_the_answer_is_an_error(bad):
    res = resolver(fake_extract(info={"channel_id": bad, "channel": "x"}))
    with pytest.raises(ProbeError):
        resolve(res, ChannelRef("handle", "@kurzgesagt"))


def test_the_name_falls_back_to_the_title_then_the_id():
    info = {"channel_id": CID, "title": "Only a title"}
    assert resolve(resolver(fake_extract(info=info)), ChannelRef("id", CID)).name == "Only a title"
    info = {"channel_id": CID}
    assert resolve(resolver(fake_extract(info=info)), ChannelRef("id", CID)).name == CID


def test_control_characters_are_removed_from_the_name():
    info = {"channel_id": CID, "channel": "Evil\x00Name\x1b[31m"}
    assert "\x00" not in resolve(resolver(fake_extract(info=info)), ChannelRef("id", CID)).name


def test_a_video_is_resolved_through_the_probe():
    info = {"title": "T", "channel": "Blender", "channel_id": CID, "duration": 10}
    res = resolver(fake_extract(info=info))
    channel = resolve(res, ChannelRef("video", VIDEO))
    assert (channel.channel_id, channel.name) == (CID, "Blender")


def test_a_video_without_a_channel_id_is_an_error():
    info = {"title": "T", "channel": "Blender", "duration": 10}
    with pytest.raises(ProbeError):
        resolve(resolver(fake_extract(info=info)), ChannelRef("video", VIDEO))


def test_a_yt_dlp_failure_keeps_its_kind_and_a_crash_becomes_unknown():
    with pytest.raises(ProbeError) as exc:
        resolve(resolver(fake_extract(error=ProbeError("network", "x"))), ChannelRef("id", CID))
    assert exc.value.kind == "network"
    with pytest.raises(ProbeError) as exc:
        resolve(resolver(fake_extract(error=KeyError("boom"))), ChannelRef("id", CID))
    assert exc.value.kind == "unknown"


def test_a_slow_lookup_times_out_and_too_many_at_once_are_refused():
    import threading

    release = threading.Event()

    def slow(url, options):
        release.wait(5)
        return channel_info()

    res = resolver(slow, timeout=0.2, max_parallel=1)

    async def scenario():
        first = asyncio.create_task(res.resolve(ChannelRef("id", CID)))
        await asyncio.sleep(0.05)
        with pytest.raises(ProbeError) as busy:
            await res.resolve(ChannelRef("id", CID))
        assert busy.value.kind == "busy"
        with pytest.raises(ProbeError) as late:
            await first
        assert late.value.kind == "timeout"

    try:
        asyncio.run(scenario())
    finally:
        release.set()


# --- the table -----------------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path) -> Iterator[Database]:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


def test_add_list_and_delete(db):
    first, created = subscriptions.add(db, CID, "Zebra")
    second, _ = subscriptions.add(db, CID2, "alpha", backfill=3)
    assert created is True
    assert first.enabled is True
    assert first.backfill == 0
    assert (first.last_checked, first.last_error) == (None, None)
    assert [s.channel_name for s in subscriptions.list_all(db)] == ["alpha", "Zebra"]
    assert second.backfill == 3
    assert subscriptions.delete(db, first.id) is True
    assert subscriptions.delete(db, first.id) is False
    assert subscriptions.get(db, first.id) is None


def test_subscribing_twice_changes_nothing(db):
    first, _ = subscriptions.add(db, CID, "One", backfill=2)
    again, created = subscriptions.add(db, CID, "Other name", backfill=9)
    assert created is False
    assert again == first
    assert len(subscriptions.list_all(db)) == 1


def test_the_number_of_subscriptions_is_limited(db, monkeypatch):
    monkeypatch.setattr(subscriptions, "MAX_SUBSCRIPTIONS", 2)
    subscriptions.add(db, "UC" + "a" * 22, "a")
    subscriptions.add(db, "UC" + "b" * 22, "b")
    with pytest.raises(subscriptions.TooManySubscriptions):
        subscriptions.add(db, "UC" + "c" * 22, "c")


def test_the_database_refuses_an_out_of_range_backfill(db):
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO subscriptions (channel_id, channel_name, backfill) VALUES (?, ?, 16)",
            (CID, "x"),
        )


def test_record_poll_sets_the_time_and_the_error_and_clears_it_again(db):
    sub, _ = subscriptions.add(db, CID, "x")
    subscriptions.record_poll(db, sub.id, "the feed was not reachable " + "x" * 600)
    got = subscriptions.get(db, sub.id)
    assert got.last_checked is not None
    assert got.last_checked.endswith("Z")
    assert len(got.last_error) == subscriptions.MAX_ERROR_LENGTH
    subscriptions.record_poll(db, sub.id)
    assert subscriptions.get(db, sub.id).last_error is None


# --- the API -------------------------------------------------------------------------------------------


@contextlib.contextmanager
def running(tmp_path: Path, extract=None) -> Iterator[TestClient]:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {**ENV, "DATA_ROOT": str(tmp_path), "CONFIG_DIR": str(config)}
    client = TestClient(create_app(Settings.from_env(values)))
    client.__enter__()
    try:
        probe = YoutubeProbe(timeout=1.0, extract=extract or fake_extract())
        client.app.state.youtube = probe
        client.app.state.channels = ChannelResolver(probe, extract=probe._extract)
        yield client
    finally:
        client.__exit__(None, None, None)


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with running(tmp_path) as test_client:
        yield test_client


def subscribe(client, channel="@kurzgesagt", **extra):
    return client.post("/api/subscriptions", json={"channel": channel, **extra})


def test_subscribing_resolves_and_stores_the_channel_id(client):
    response = subscribe(client)
    assert response.status_code == 201
    body = response.json()
    assert body["channel_id"] == CID
    assert body["channel_name"] == "Kurzgesagt - In a Nutshell"
    assert body["channel_url"] == f"https://www.youtube.com/channel/{CID}"
    assert body["duplicate"] is False
    assert body["backfill"] == 0
    assert body["last_checked"] is None
    assert body["last_error"] is None
    assert body["enabled"] is True
    listing = client.get("/api/subscriptions").json()["subscriptions"]
    assert [s["channel_id"] for s in listing] == [CID]
    assert "duplicate" not in listing[0]


def test_the_same_channel_again_is_a_200_duplicate_even_via_another_address(client):
    assert subscribe(client).status_code == 201
    again = subscribe(client, f"https://www.youtube.com/channel/{CID}")
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert len(client.get("/api/subscriptions").json()["subscriptions"]) == 1


def test_an_explicit_name_and_backfill_are_used(client):
    body = subscribe(client, name="  Kurzgesagt  ", backfill=5).json()
    assert body["channel_name"] == "Kurzgesagt"
    assert body["backfill"] == 5


@pytest.mark.security
@pytest.mark.parametrize(
    ("youtube_name", "folder"),
    [
        ("AC/DC", "AC-DC"),
        ("..\\..\\evil", "..-..-evil"),
        ("Tab\tand\nnewline", "Tabandnewline"),  # the lookup already drops control characters
        ("...", CID),
        ("", CID),
    ],
)
def test_a_channel_name_from_youtube_becomes_a_safe_folder_name(tmp_path, youtube_name, folder):
    info = {"channel_id": CID, "channel": youtube_name}
    with running(tmp_path, fake_extract(info=info)) as client:
        assert (
            subscribe(client, f"https://www.youtube.com/channel/{CID}").json()["channel_name"]
            == folder
        )


def test_a_video_address_subscribes_to_its_channel(tmp_path):
    info = {"title": "T", "channel": "Blender", "channel_id": CID, "duration": 10}
    with running(tmp_path, fake_extract(info=info)) as client:
        body = subscribe(client, f"https://youtu.be/{VIDEO}").json()
        assert (body["channel_id"], body["channel_name"]) == (CID, "Blender")


@pytest.mark.parametrize(
    ("body", "field", "kind"),
    [
        ({"channel": "https://example.com/x"}, "channel", "channel_invalid"),
        ({"channel": ""}, "channel", "channel_invalid"),
        ({}, "channel", "missing"),
        ({"channel": "@kurzgesagt", "name": "a/b"}, "name", "name_path_separator"),
        ({"channel": "@kurzgesagt", "name": " "}, "name", "name_empty"),
        ({"channel": "@kurzgesagt", "backfill": 16}, "backfill", "less_than_equal"),
        ({"channel": "@kurzgesagt", "backfill": -1}, "backfill", "greater_than_equal"),
        ({"channel": "@kurzgesagt", "backfill": True}, "backfill", "int_type"),
        ({"channel": "@kurzgesagt", "backfill": "3"}, "backfill", "int_type"),
        ({"channel": "@kurzgesagt", "extra": 1}, "extra", "extra_forbidden"),
    ],
)
def test_invalid_input_is_a_field_level_422_and_nothing_is_stored(client, body, field, kind):
    response = client.post("/api/subscriptions", json=body)
    assert response.status_code == 422
    errors = {e["loc"][-1]: e["type"] for e in response.json()["detail"]}
    assert errors[field] == kind
    assert client.get("/api/subscriptions").json() == {"subscriptions": []}


@pytest.mark.parametrize(
    ("kind", "status"),
    [("private", 404), ("unavailable", 404), ("network", 502), ("timeout", 504), ("busy", 429)],
)
def test_lookup_failures_keep_their_kind_and_store_nothing(tmp_path, kind, status):
    with running(tmp_path, fake_extract(error=ProbeError(kind, "nope"))) as client:
        response = subscribe(client)
        assert response.status_code == status
        assert response.json() == {"detail": "nope", "kind": kind}
        assert client.get("/api/subscriptions").json()["subscriptions"] == []


def test_the_limit_is_a_409(client, monkeypatch):
    monkeypatch.setattr(subscriptions, "MAX_SUBSCRIPTIONS", 0)
    assert subscribe(client).status_code == 409


def test_deleting_a_subscription(client):
    sub_id = subscribe(client).json()["id"]
    response = client.delete(f"/api/subscriptions/{sub_id}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/subscriptions").json()["subscriptions"] == []
    assert client.delete(f"/api/subscriptions/{sub_id}").status_code == 404
    assert client.delete("/api/subscriptions/abc").status_code == 422


def test_the_list_shows_last_checked_and_last_error(client):
    sub_id = subscribe(client).json()["id"]
    subscriptions.record_poll(client.app.state.db, sub_id, "feed not reachable")
    row = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert row["last_error"] == "feed not reachable"
    assert row["last_checked"].endswith("Z")


def test_without_a_database_every_route_is_a_503(tmp_path):
    app = create_app(Settings.from_env({**ENV, "DATA_ROOT": str(tmp_path)}))
    client = TestClient(app)
    assert client.get("/api/subscriptions").status_code == 503
    assert client.post("/api/subscriptions", json={"channel": "@kurzgesagt"}).status_code == 503
    assert client.delete("/api/subscriptions/1").status_code == 503


@pytest.mark.security
def test_cross_site_requests_to_the_subscription_routes_are_refused(client):
    sub_id = subscribe(client).json()["id"]
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert (
        client.post("/api/subscriptions", json={"channel": CID2}, headers=cross).status_code == 403
    )
    assert client.delete(f"/api/subscriptions/{sub_id}", headers=cross).status_code == 403
    evil = {"Origin": "http://evil.example"}
    assert client.delete(f"/api/subscriptions/{sub_id}", headers=evil).status_code == 403
    assert [s["id"] for s in client.get("/api/subscriptions").json()["subscriptions"]] == [sub_id]


def test_the_status_keys_of_a_lookup_failure_are_all_known():
    from ytp.apiutil import PROBE_STATUS

    for kind in ("private", "unavailable", "network", "timeout", "busy", "unknown", "unsupported"):
        assert kind in PROBE_STATUS
