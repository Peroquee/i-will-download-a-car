"""app/ytp/clients/qbittorrent.py against a fake qBittorrent Web API (no real one in CI)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from tests.helpers.fake_qbittorrent import PASSWORD, USER, FakeQbittorrent
from tests.helpers.net import free_port
from ytp.clients.qbittorrent import (
    CATEGORIES,
    QbitAuthError,
    QbitClient,
    QbitRequestError,
    QbitUnavailable,
    Torrent,
)
from ytp.magnet import InvalidMagnet

HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Debian%20ISO&tr=udp%3A%2F%2Ftracker.example%3A1%2Fannounce"


@pytest.fixture
def fake() -> Iterator[FakeQbittorrent]:
    server = FakeQbittorrent().start()
    yield server
    server.stop()


def client_for(fake: FakeQbittorrent, **kwargs) -> QbitClient:
    kwargs.setdefault("timeout", 3.0)
    return QbitClient(
        fake.url, kwargs.pop("user", USER), kwargs.pop("password", PASSWORD), **kwargs
    )


# --- login ---------------------------------------------------------------------------


def test_modern_login_answers_204_with_no_body(fake):
    assert client_for(fake).version() == "v5.0.4"
    assert fake.logins == 1


def test_legacy_login_answers_200_ok(fake):
    fake.login_style = "legacy"
    assert client_for(fake).version() == "v5.0.4"


def test_the_session_cookie_is_sent_on_later_calls_and_login_happens_once(fake):
    client = client_for(fake)
    client.version()
    client.version()
    assert fake.logins == 1
    assert all(s.headers["cookie"] == fake.cookie for s in fake.calls("app/version"))


@pytest.mark.parametrize("name", ["SID", "QBT_SID_8080", "QBT_SID_1"])
def test_the_session_cookie_name_of_any_generation_is_accepted_and_sent_back(fake, name):
    """qBittorrent 5.2 renamed SID to QBT_SID_<port>; the client must not care."""
    fake.cookie_name = name
    client = client_for(fake)
    assert client.version() == "v5.0.4"
    assert fake.calls("app/version")[0].headers["cookie"] == f"{name}=SID-0001"


def test_only_a_session_cookie_counts_other_cookies_in_the_login_answer_are_ignored(fake):
    fake.cookie_name = "tracking"
    with pytest.raises(QbitUnavailable, match="no session cookie"):
        client_for(fake).version()


def test_login_posts_the_credentials_as_a_form(fake):
    client_for(fake).login()
    (login,) = fake.calls("auth/login")
    assert login.method == "POST"
    assert login.form == {"username": [USER], "password": [PASSWORD]}


def test_host_origin_and_referer_match_the_base_url(fake):
    """qBittorrent answers 401 for a Host/port mismatch and checks Origin/Referer against Host."""
    client_for(fake).version()
    for seen in fake.seen:
        assert seen.headers["host"] == f"127.0.0.1:{fake.port}"
        assert seen.headers["origin"] == fake.url
        assert seen.headers["referer"] == fake.url + "/"


@pytest.mark.parametrize("style", ["modern", "legacy"])
def test_wrong_password_is_an_auth_error_in_both_generations(fake, style):
    fake.login_style = style
    client = client_for(fake, password="wrong password")
    with pytest.raises(QbitAuthError, match="wrong user or password"):
        client.version()


def test_a_refused_login_is_never_retried_automatically(fake):
    """Five failed logins ban the address for an hour: one failure, then silence."""
    client = client_for(fake, password="wrong password")
    for _ in range(4):
        with pytest.raises(QbitAuthError):
            client.version()
    assert fake.logins == 1
    assert len(fake.seen) == 1


def test_wrong_credentials_stay_refused_even_much_later(fake):
    now = [0.0]
    client = client_for(fake, password="wrong password", clock=lambda: now[0])
    with pytest.raises(QbitAuthError):
        client.version()
    now[0] += 10 * 3600
    with pytest.raises(QbitAuthError):
        client.version()
    assert fake.logins == 1


def test_reset_login_block_allows_one_new_attempt(fake):
    client = client_for(fake, password="wrong password")
    with pytest.raises(QbitAuthError):
        client.version()
    client._password = PASSWORD  # the credentials were fixed
    client.reset_login_block()
    assert client.version() == "v5.0.4"


def test_a_ban_is_reported_and_waits_an_hour_before_the_next_try(fake):
    fake.login_style = "banned"
    now = [1000.0]
    client = client_for(fake, clock=lambda: now[0])
    with pytest.raises(QbitAuthError, match="banned"):
        client.version()
    now[0] += 3599
    with pytest.raises(QbitAuthError, match="banned"):
        client.version()
    assert fake.logins == 1
    now[0] += 2
    fake.login_style = "modern"
    assert client.version() == "v5.0.4"
    assert fake.logins == 2


def test_missing_password_fails_clearly_without_any_network_traffic(fake):
    client = client_for(fake, password="")
    with pytest.raises(QbitAuthError, match="QBIT_PASSWORD is not set"):
        client.version()
    assert fake.seen == []


def test_401_on_an_api_call_points_at_the_url(fake):
    client = client_for(fake)
    client.login()
    fake.fail_status = 401
    with pytest.raises(QbitAuthError, match="Host/port"):
        client.version()


# --- expired session -------------------------------------------------------------------


def test_an_expired_session_logs_in_again_once_and_repeats_the_call(fake, caplog):
    client = client_for(fake)
    client.version()
    fake.expire_session()
    with caplog.at_level(logging.WARNING, logger="ytp.clients.qbittorrent"):
        assert client.version() == "v5.0.4"
    assert fake.logins == 2
    assert [s.headers.get("cookie") for s in fake.calls("app/version")] == [
        f"QBT_SID_{fake.port}=SID-0001",
        f"QBT_SID_{fake.port}=SID-0001",
        f"QBT_SID_{fake.port}=SID-0002",
    ]
    assert "session expired" in caplog.text


def test_a_session_that_keeps_being_rejected_does_not_loop(fake):
    client = client_for(fake)
    client.login()
    fake.reject_sessions = True
    with pytest.raises(QbitAuthError, match="even after a new login"):
        client.version()
    assert fake.logins == 2, "exactly one re-login"
    assert len(fake.calls("app/version")) == 2, "exactly one repeat"


def test_a_post_is_repeated_with_the_same_body_after_re_login(fake):
    client = client_for(fake)
    client.login()
    fake.expire_session()
    client.add_magnet(MAGNET, "movies")
    first, second = fake.calls("torrents/add")
    assert first.form == second.form
    assert first.headers["cookie"] != second.headers["cookie"]


def test_concurrent_first_calls_log_in_only_once(fake):
    client = client_for(fake)
    errors: list[BaseException] = []

    def call():
        try:
            client.version()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert fake.logins == 1


# --- unreachable -----------------------------------------------------------------------


def all_calls(client: QbitClient):
    return [
        client.version,
        client.login,
        lambda: client.add_magnet(MAGNET, "movies"),
        client.list_torrents,
        lambda: client.list_torrents("downloading"),
        client.pause_all,
        client.resume_all,
    ]


def test_connection_refused_is_qbit_unavailable_for_every_call():
    client = QbitClient(f"http://127.0.0.1:{free_port()}", USER, PASSWORD, timeout=2)
    for call in all_calls(client):
        with pytest.raises(QbitUnavailable, match="cannot reach qBittorrent"):
            call()


def test_unknown_host_is_qbit_unavailable():
    client = QbitClient("http://no-such-host.invalid:8080", USER, PASSWORD, timeout=3)
    with pytest.raises(QbitUnavailable):
        client.version()


def test_a_server_that_never_answers_times_out_quickly():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)  # accepts connections into the backlog, never reads or replies
        client = QbitClient(
            f"http://127.0.0.1:{sock.getsockname()[1]}", USER, PASSWORD, timeout=0.5
        )
        started = time.monotonic()
        with pytest.raises(QbitUnavailable):
            client.version()
    assert time.monotonic() - started < 3


def test_a_connection_closed_without_an_answer_is_qbit_unavailable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        port = sock.getsockname()[1]

        def hang_up():
            conn, _ = sock.accept()
            conn.close()

        thread = threading.Thread(target=hang_up)
        thread.start()
        client = QbitClient(f"http://127.0.0.1:{port}", USER, PASSWORD, timeout=2)
        with pytest.raises(QbitUnavailable):
            client.version()
        thread.join()


def test_a_server_error_is_qbit_unavailable_and_does_not_look_like_a_bad_login(fake):
    client = client_for(fake)
    client.login()
    fake.fail_status = 503
    with pytest.raises(QbitUnavailable, match="503"):
        client.version()


@pytest.mark.parametrize("style", ["weird", "nocookie"])
def test_an_unexpected_login_answer_is_qbit_unavailable_not_a_bad_password(fake, style):
    fake.login_style = style
    client = client_for(fake)
    with pytest.raises(QbitUnavailable):
        client.version()
    # Not a credentials problem, so the next call tries again.
    fake.login_style = "modern"
    assert client.version() == "v5.0.4"


def test_qbittorrent_coming_back_works_without_restarting_the_app(fake):
    """The VPN drops, qBittorrent is gone for a while, then it is back with new sessions."""
    client = client_for(fake)
    client.version()
    port = fake.port
    fake.stop()
    with pytest.raises(QbitUnavailable):
        client.version()
    restarted = FakeQbittorrent().start(port)
    try:
        assert client.version() == "v5.0.4", "stale cookie: 403, one re-login, then fine"
        assert restarted.logins == 1
    finally:
        restarted.stop()


# --- add magnet --------------------------------------------------------------------------


def test_add_magnet_sends_link_category_and_auto_tmm_as_form_fields(fake):
    infohash = client_for(fake).add_magnet(MAGNET, "series")
    assert infohash == HASH
    (call,) = fake.calls("torrents/add")
    assert call.method == "POST"
    assert call.form == {"urls": [MAGNET], "category": ["series"], "autoTMM": ["true"]}
    assert call.query == {}, "the link is never part of the URL"


@pytest.mark.parametrize("category", CATEGORIES)
def test_every_known_category_is_accepted(fake, category):
    client_for(fake).add_magnet(MAGNET, category)
    assert fake.calls("torrents/add")[0].form["category"] == [category]


def test_a_rejected_magnet_is_a_request_error_not_a_success(fake):
    fake.add_result = "Fails."
    with pytest.raises(QbitRequestError, match=HASH):
        client_for(fake).add_magnet(MAGNET, "movies")


@pytest.mark.security
@pytest.mark.parametrize(
    "magnet",
    [
        "http://example.com/a.torrent",
        f"{MAGNET}\nhttp://169.254.169.254/",
        "",
        "magnet:?dn=nothing",
    ],
)
def test_untrusted_input_is_refused_before_anything_is_sent(fake, magnet):
    with pytest.raises(InvalidMagnet):
        client_for(fake).add_magnet(magnet, "movies")
    assert fake.seen == []


@pytest.mark.parametrize(
    "category", ["", "youtube", "Movies", "movies ", "../etc", "movies\nmusic"]
)
def test_an_unknown_category_is_refused_before_anything_is_sent(fake, category):
    with pytest.raises(ValueError, match="category"):
        client_for(fake).add_magnet(MAGNET, category)
    assert fake.seen == []


# --- list torrents -------------------------------------------------------------------------


def sample(hash_: str, name: str, **extra) -> dict:
    return {
        "hash": hash_,
        "name": name,
        "state": "downloading",
        "progress": 0.5,
        "category": "movies",
        "save_path": "/data/downloads/movies",
        "content_path": f"/data/downloads/movies/{name}",
        "size": 1000,
        "amount_left": 500,
        "added_on": 1700000000,
        "completion_on": -1,
        "unknown_field": "ignored",
        **extra,
    }


def test_list_torrents_parses_the_fields_we_use(fake):
    fake.torrents = [sample("AAAA" + "0" * 36, "Größe Reise.mkv")]
    (torrent,) = client_for(fake).list_torrents()
    assert torrent == Torrent(
        hash="aaaa" + "0" * 36,
        name="Größe Reise.mkv",
        state="downloading",
        progress=0.5,
        category="movies",
        save_path="/data/downloads/movies",
        content_path="/data/downloads/movies/Größe Reise.mkv",
        size=1000,
        amount_left=500,
        added_on=1700000000,
        completion_on=-1,
    )
    assert not torrent.finished


def test_a_finished_torrent_and_missing_optional_fields(fake):
    fake.torrents = [{"hash": HASH, "name": "x", "state": "uploading", "progress": 1}]
    (torrent,) = client_for(fake).list_torrents()
    assert torrent.finished
    assert torrent.category == ""
    assert torrent.size == 0


@pytest.mark.parametrize(
    ("state", "paused"),
    [
        ("stoppedDL", True),
        ("stoppedUP", True),
        ("pausedDL", True),
        ("pausedUP", True),
        ("downloading", False),
        ("stalledDL", False),
        ("queuedDL", False),
        ("uploading", False),
    ],
)
def test_paused_is_decided_by_the_state_not_by_a_filter(state, paused):
    torrent = Torrent.from_api({"hash": HASH, "name": "x", "state": state, "progress": 0.5})
    assert torrent.paused is paused


@pytest.mark.parametrize(
    ("state", "transferring"),
    [
        ("downloading", True),
        ("forcedDL", True),
        ("metaDL", True),
        ("forcedMetaDL", True),
        ("stalledDL", False),
        ("queuedDL", False),
        ("pausedDL", False),
        ("stoppedDL", False),
        ("checkingDL", False),
        ("uploading", False),
    ],
)
def test_transferring_is_decided_by_the_state(state, transferring):
    torrent = Torrent.from_api({"hash": HASH, "name": "x", "state": state, "progress": 0.5})
    assert torrent.transferring is transferring


@pytest.mark.parametrize(
    ("state", "queued"),
    [("queuedDL", True), ("queuedUP", True), ("downloading", False), ("pausedDL", False)],
)
def test_queued_is_decided_by_the_state(state, queued):
    torrent = Torrent.from_api({"hash": HASH, "name": "x", "state": state, "progress": 0.5})
    assert torrent.queued is queued


def test_no_filter_sends_no_filter_parameter(fake):
    client_for(fake).list_torrents()
    assert fake.calls("torrents/info")[0].query == {}
    client_for(fake).list_torrents("all")
    assert fake.calls("torrents/info")[1].query == {}


@pytest.mark.parametrize("state", ["downloading", "completed", "seeding", "stalled_uploading"])
def test_state_filter_is_sent_as_the_filter_parameter(fake, state):
    fake.torrents = [sample(HASH, "a", _filters=[state])]
    assert len(client_for(fake).list_torrents(state)) == 1
    assert fake.calls("torrents/info")[0].query == {"filter": [state]}


def test_category_and_hashes_are_sent_as_parameters(fake):
    other = "f" * 40
    fake.torrents = [sample(HASH, "a"), sample(other, "b", category="music")]
    result = client_for(fake).list_torrents(category="music", hashes=[other.upper(), HASH])
    assert [t.hash for t in result] == [other]
    query = fake.calls("torrents/info")[0].query
    assert query["category"] == ["music"]
    assert query["hashes"] == [f"{other}|{HASH}"]


def test_unknown_state_is_refused_before_anything_is_sent(fake):
    with pytest.raises(ValueError, match="state"):
        client_for(fake).list_torrents("bogus&hashes=all")
    assert fake.seen == []


@pytest.mark.parametrize(("asked", "alternate"), [("paused", "stopped"), ("resumed", "running")])
def test_4x_and_5x_state_names_fall_back_on_400(fake, asked, alternate):
    fake.bad_filters = {asked}
    fake.torrents = [sample(HASH, "a", _filters=[alternate])]
    assert len(client_for(fake).list_torrents(asked)) == 1
    assert [c.query["filter"] for c in fake.calls("torrents/info")] == [[asked], [alternate]]


def test_a_400_for_another_filter_is_a_request_error(fake):
    fake.bad_filters = {"downloading"}
    with pytest.raises(QbitRequestError):
        client_for(fake).list_torrents("downloading")


def test_garbled_lists_are_qbit_unavailable(fake):
    client = client_for(fake)
    client.login()
    fake.torrents = []
    for bad in ({"not": "a list"}, [{"name": "no hash"}]):
        fake.torrents = bad  # type: ignore[assignment]
        with pytest.raises(QbitUnavailable):
            client.list_torrents()


# --- pause all -------------------------------------------------------------------------------


def test_pause_all_uses_stop_on_qbittorrent_5(fake):
    client_for(fake).pause_all()
    (call,) = fake.calls("torrents/stop")
    assert call.form == {"hashes": ["all"]}
    assert fake.calls("torrents/pause") == []


def test_pause_all_falls_back_to_pause_on_qbittorrent_4(fake):
    fake.api_generation = "4"
    client_for(fake).pause_all()
    assert len(fake.calls("torrents/stop")) == 1
    (call,) = fake.calls("torrents/pause")
    assert call.form == {"hashes": ["all"]}


def test_resume_all_is_the_mirror_image(fake):
    client_for(fake).resume_all()
    assert len(fake.calls("torrents/start")) == 1
    assert fake.calls("torrents/resume") == []
    fake.api_generation = "4"
    client_for(fake).resume_all()
    assert len(fake.calls("torrents/resume")) == 1


def test_neither_name_existing_is_a_request_error(fake):
    fake.api_generation = "none"
    with pytest.raises(QbitRequestError):
        client_for(fake).pause_all()


# --- secrets and construction ---------------------------------------------------------------------


@pytest.mark.security
def test_password_and_session_cookie_never_reach_logs_errors_or_repr(fake, caplog):
    secret_pw = "never-in-a-log-password"
    caplog.set_level(logging.DEBUG)
    good = client_for(fake)
    good.version()
    fake.expire_session()
    good.version()
    bad = client_for(fake, password=secret_pw)
    with pytest.raises(QbitAuthError) as auth_error:
        bad.version()
    dead = QbitClient(f"http://127.0.0.1:{free_port()}", USER, secret_pw, timeout=1)
    with pytest.raises(QbitUnavailable) as unavailable:
        dead.version()
    everything = " ".join(
        [caplog.text, repr(good), repr(bad), str(auth_error.value), str(unavailable.value)]
    )
    for secret in (secret_pw, PASSWORD, "SID-0001", "SID-0002"):
        assert secret not in everything, secret


@pytest.mark.parametrize(
    "url", ["", "ftp://x", "gluetun:8080", "http://", "http://gluetun:8080/qbt", "http:///x"]
)
def test_a_bad_base_url_is_refused(url):
    with pytest.raises(ValueError, match="base_url"):
        QbitClient(url, USER, PASSWORD)


def test_a_trailing_slash_and_the_default_port_are_normalised():
    assert QbitClient("http://gluetun:8080/", USER, PASSWORD).base_url == "http://gluetun:8080"
    assert QbitClient("http://gluetun:80", USER, PASSWORD).base_url == "http://gluetun"
    assert QbitClient("https://q.example", USER, PASSWORD).base_url == "https://q.example"


def test_from_settings_uses_url_user_and_password():
    from ytp.config import Settings

    settings = Settings(qbit_url="http://gluetun:8080", qbit_user="u", qbit_password="p" * 12)
    client = QbitClient.from_settings(settings)
    assert client.base_url == "http://gluetun:8080"
    assert "p" * 12 not in repr(client)


def test_recheck_torrents_sends_the_hashes_in_the_body(fake):
    client_for(fake).recheck_torrents(["A" * 40])
    call = fake.calls("torrents/recheck")[0]
    assert call.method == "POST"
    assert call.form == {"hashes": ["a" * 40]}
    assert call.query == {}


def test_recheck_torrents_reports_a_refusal(fake):
    fake.fail_status = 500
    with pytest.raises(QbitUnavailable):
        client_for(fake).recheck_torrents(["a" * 40])


def test_delete_torrents_sends_the_hash_and_the_files_flag_in_the_body(fake):
    fake.torrents = [{"hash": "a" * 40}, {"hash": "b" * 40}]
    client_for(fake).delete_torrents(["A" * 40], delete_files=True)
    call = fake.calls("torrents/delete")[0]
    assert call.method == "POST"
    assert call.form == {"hashes": ["a" * 40], "deleteFiles": ["true"]}
    assert call.query == {}
    assert [t["hash"] for t in fake.torrents] == ["b" * 40]
    client_for(fake).delete_torrents(["b" * 40], delete_files=False)
    assert fake.calls("torrents/delete")[1].form["deleteFiles"] == ["false"]


def test_delete_torrents_refuses_anything_that_is_not_an_infohash(fake):
    for bad in ([], ["all"], ["a" * 40 + "|" + "b" * 40], ["../x"]):
        with pytest.raises(ValueError, match="infohash"):
            client_for(fake).delete_torrents(bad, delete_files=True)
    assert fake.calls("torrents/delete") == []


def test_delete_torrents_reports_a_refusal(fake):
    fake.delete_status = 500
    with pytest.raises(QbitUnavailable):  # a 5xx answer, like every other call
        client_for(fake).delete_torrents(["a" * 40], delete_files=True)
