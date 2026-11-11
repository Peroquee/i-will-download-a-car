"""Login, logout, sessions, passwords, account management and the brake on guessing (#44)."""

from __future__ import annotations

import logging

import pytest

from tests.helpers.accounts_app import ADMIN, PASSWORD, add_account, client_for, db_of, running
from ytp import accounts
from ytp.auth import COOKIE, LoginLimiter

pytestmark = [pytest.mark.real_auth, pytest.mark.security]

NEW_PASSWORD = "another long password"  # a test value, not a secret


@pytest.fixture
def app_client(tmp_path):
    with running(tmp_path) as client:
        yield client


def login(browser, name=ADMIN, password=PASSWORD, **headers):
    return browser.post(
        "/api/auth/login", json={"username": name, "password": password}, headers=headers
    )


def token_of(browser) -> str | None:
    """The cookie the server set (the jar may also hold one a test put there by hand)."""
    values = [c.value for c in browser.cookies.jar if c.name == COOKIE and c.domain]
    return values[-1] if values else None


def plant(browser, value: str) -> None:
    """A cookie in the browser as the server would have set it (for its host, whole site)."""
    browser.cookies.set(COOKIE, value, domain="testserver.local", path="/")


# --- login ---


def test_a_login_gives_a_session_cookie_that_is_httponly_and_samesite_strict(app_client):
    response = login(client_for(app_client, None))
    assert response.status_code == 200
    assert response.json()["account"]["username"] == ADMIN
    assert response.json()["account"]["role"] == "admin"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    flags = cookie.lower()
    assert "httponly" in flags
    assert "samesite=strict" in flags
    assert "path=/" in flags
    assert "max-age=" in flags
    assert response.headers["cache-control"] == "no-store"


def test_a_login_never_returns_a_password_or_its_hash(app_client):
    body = login(client_for(app_client, None)).text
    assert "hash" not in body.lower()
    assert "scrypt" not in body
    assert PASSWORD not in body


def test_the_session_works_for_later_requests_and_me_says_who(app_client):
    browser = client_for(app_client, None)
    assert browser.get("/api/jobs").status_code == 401
    assert login(browser).status_code == 200
    assert browser.get("/api/jobs").status_code == 200
    me = browser.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["account"]["username"] == ADMIN
    assert client_for(app_client, None).get("/api/auth/me").status_code == 401


def test_the_token_is_long_random_and_only_its_hash_is_stored(app_client):
    browser = client_for(app_client, None)
    login(browser)
    token = token_of(browser)
    assert len(token) >= 40
    rows = db_of(app_client).query("SELECT token_hash FROM sessions")
    assert [r["token_hash"] for r in rows] == [accounts.token_hash(token)]
    assert token not in rows[0]["token_hash"]


def test_a_user_name_is_not_case_sensitive(app_client):
    assert login(client_for(app_client, None), "BOSS").status_code == 200


def test_a_wrong_password_an_unknown_name_and_a_disabled_account_look_the_same(app_client):
    add_account(app_client, "alice")
    off = add_account(app_client, "bob")
    accounts.set_disabled(db_of(app_client), off.id, True)
    answers = []
    for name, password in [(ADMIN, "wrong password!!"), ("nobody", PASSWORD), ("bob", PASSWORD)]:
        browser = client_for(app_client, None)
        response = login(browser, name, password)
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert token_of(browser) is None
        answers.append(response.json())
    assert answers[0] == answers[1] == answers[2] == {"detail": "wrong name or password"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"username": ADMIN},
        {"password": PASSWORD},
        {"username": 1, "password": PASSWORD},
        {"username": ADMIN, "password": ["a", "b"]},
        {"username": ADMIN, "password": ""},
        {"username": "", "password": PASSWORD},
        [],
        "text",
    ],
)
def test_a_malformed_login_is_a_422_that_does_not_echo_the_input(app_client, body):
    response = client_for(app_client, None).post("/api/auth/login", json=body)
    assert response.status_code == 422
    assert PASSWORD not in response.text


def test_a_login_that_is_not_json_or_far_too_large_is_refused(app_client):
    browser = client_for(app_client, None)
    assert browser.post("/api/auth/login", content=b"user=boss&password=x").status_code == 422
    assert browser.post("/api/auth/login", json={"username": "x" * 10_000}).status_code == 413


def test_a_overlong_password_is_a_wrong_password_not_a_crash(app_client):
    response = login(client_for(app_client, None), ADMIN, "p" * 250)
    assert response.status_code == 401


def test_a_login_records_the_time(app_client):
    assert accounts.find(db_of(app_client), ADMIN)[0].last_login is None
    login(client_for(app_client, None))
    assert accounts.find(db_of(app_client), ADMIN)[0].last_login is not None


# --- session fixation ---


def test_every_login_makes_a_new_token(app_client):
    first, second = client_for(app_client, None), client_for(app_client, None)
    login(first)
    login(second)
    assert token_of(first) != token_of(second)
    assert first.get("/api/jobs").status_code == 200
    assert second.get("/api/jobs").status_code == 200


def test_a_token_that_came_with_the_login_is_never_adopted(app_client):
    chosen = "attacker-chosen-token-" + "a" * 30
    browser = client_for(app_client, None)
    plant(browser, chosen)
    response = login(browser)
    assert response.status_code == 200
    assert token_of(browser) != chosen
    attacker = client_for(app_client, None)
    plant(attacker, chosen)
    assert attacker.get("/api/jobs").status_code == 401


def test_logging_in_again_ends_the_session_that_was_presented(app_client):
    browser = client_for(app_client, None)
    login(browser)
    old = token_of(browser)
    login(browser)
    assert token_of(browser) != old
    stale = client_for(app_client, None)
    stale.cookies.set(COOKIE, old)
    assert stale.get("/api/jobs").status_code == 401
    assert browser.get("/api/jobs").status_code == 200


def test_a_failed_login_keeps_the_present_session_and_changes_nothing(app_client):
    browser = client_for(app_client, None)
    login(browser)
    old = token_of(browser)
    assert login(browser, ADMIN, "wrong password!!").status_code == 401
    assert token_of(browser) == old
    assert browser.get("/api/jobs").status_code == 200


# --- logout ---


def test_logout_ends_the_session_on_the_server_and_clears_the_cookie(app_client):
    browser = client_for(app_client, None)
    login(browser)
    old = token_of(browser)
    response = browser.post("/api/auth/logout")
    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert accounts.session_account(db_of(app_client), old) is None
    # replaying the old cookie (a copy somebody kept) does not work any more
    replay = client_for(app_client, None)
    replay.cookies.set(COOKIE, old)
    assert replay.get("/api/jobs").status_code == 401


def test_logout_ends_only_this_session(app_client):
    one, two = client_for(app_client, None), client_for(app_client, None)
    login(one)
    login(two)
    one.post("/api/auth/logout")
    assert two.get("/api/jobs").status_code == 200


def test_logout_needs_a_login_and_a_same_origin_request(app_client):
    assert client_for(app_client, None).post("/api/auth/logout").status_code == 401
    browser = client_for(app_client, None)
    login(browser)
    cross = browser.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403
    assert browser.get("/api/jobs").status_code == 200


# --- CSRF ---


@pytest.mark.parametrize("origin", ["null", "http://evil.example", "http://testserver:9999"])
def test_a_cross_site_post_is_refused_before_anything_happens(app_client, origin):
    browser = client_for(app_client, None)
    assert login(browser, Origin=origin).status_code == 403
    assert token_of(browser) is None
    assert db_of(app_client).query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 0


def test_the_own_origin_is_fine(app_client):
    assert login(client_for(app_client, None), Origin="http://testserver").status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/accounts", {"username": "mallory", "password": PASSWORD}),
        ("PATCH", "/api/accounts/1", {"role": "user"}),
        ("POST", "/api/accounts/1/password", {"password": NEW_PASSWORD}),
        ("DELETE", "/api/accounts/1", None),
        ("POST", "/api/auth/password", {"current": PASSWORD, "new": NEW_PASSWORD}),
    ],
)
def test_a_logged_in_admin_cannot_be_made_to_change_accounts_from_another_site(
    app_client, method, path, body
):
    browser = client_for(app_client, ADMIN)
    kwargs = {"json": body} if body is not None else {}
    response = browser.request(method, path, headers={"Origin": "null"}, **kwargs)
    assert response.status_code == 403
    assert [a.username for a in accounts.list_all(db_of(app_client))] == [ADMIN]


# --- the own password ---


def change(browser, current=PASSWORD, new=NEW_PASSWORD):
    return browser.post("/api/auth/password", json={"current": current, "new": new})


def test_changing_the_password_ends_the_other_sessions_but_not_this_one(app_client):
    add_account(app_client, "alice")
    here, elsewhere = client_for(app_client, None), client_for(app_client, None)
    login(here, "alice")
    login(elsewhere, "alice")
    assert change(here).status_code == 200
    assert here.get("/api/jobs").status_code == 200
    assert elsewhere.get("/api/jobs").status_code == 401
    assert login(client_for(app_client, None), "alice", PASSWORD).status_code == 401
    assert login(client_for(app_client, None), "alice", NEW_PASSWORD).status_code == 200


def test_the_current_password_must_be_right(app_client):
    add_account(app_client, "alice")
    browser = client_for(app_client, None)
    login(browser, "alice")
    response = change(browser, current="not the password")
    assert response.status_code == 403
    assert login(client_for(app_client, None), "alice", PASSWORD).status_code == 200


def test_a_weak_new_password_is_refused_and_not_echoed(app_client):
    add_account(app_client, "alice")
    browser = client_for(app_client, None)
    login(browser, "alice")
    response = change(browser, new="short")
    assert response.status_code == 422
    assert "short" not in response.json()["detail"].replace("the password needs", "")
    assert login(client_for(app_client, None), "alice", PASSWORD).status_code == 200


def test_guessing_the_current_password_is_braked_like_a_login(app_client):
    add_account(app_client, "alice")
    browser = client_for(app_client, None)
    login(browser, "alice")
    codes = [change(browser, current=f"wrong password {i}").status_code for i in range(7)]
    assert codes[:5] == [403] * 5
    assert codes[5:] == [429, 429]
    assert change(browser).status_code == 429  # even the right one waits


# --- accounts, by an admin ---


def test_an_admin_creates_a_user_who_can_then_log_in(app_client):
    admin = client_for(app_client, ADMIN)
    response = admin.post(
        "/api/accounts", json={"username": "alice", "password": PASSWORD, "role": "user"}
    )
    assert response.status_code == 201
    assert response.json()["account"]["role"] == "user"
    assert "password" not in response.text.lower().replace("password_", "")
    assert login(client_for(app_client, None), "alice").status_code == 200
    listed = admin.get("/api/accounts").json()["accounts"]
    assert sorted(a["username"] for a in listed) == ["alice", ADMIN]
    assert all("hash" not in a and "password" not in a for a in listed)


def test_the_role_defaults_to_user(app_client):
    admin = client_for(app_client, ADMIN)
    created = admin.post("/api/accounts", json={"username": "alice", "password": PASSWORD})
    assert created.json()["account"]["role"] == "user"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"username": "al", "password": PASSWORD}, 422),
        ({"username": "has space", "password": PASSWORD}, 422),
        ({"username": "alice", "password": "short"}, 422),
        ({"username": "alice", "password": PASSWORD, "role": "root"}, 422),
        ({"username": ADMIN.upper(), "password": PASSWORD}, 409),
    ],
)
def test_bad_account_requests_are_refused(app_client, body, status):
    admin = client_for(app_client, ADMIN)
    response = admin.post("/api/accounts", json=body)
    assert response.status_code == status
    assert PASSWORD not in response.text
    assert len(accounts.list_all(db_of(app_client))) == 1


def test_a_user_cannot_manage_accounts(app_client):
    alice = add_account(app_client, "alice")
    browser = client_for(app_client, alice)
    assert browser.get("/api/accounts").status_code == 403
    assert (
        browser.post("/api/accounts", json={"username": "eve", "password": PASSWORD}).status_code
        == 403
    )
    assert browser.patch(f"/api/accounts/{alice.id}", json={"role": "admin"}).status_code == 403
    assert (
        browser.post(
            f"/api/accounts/{alice.id}/password", json={"password": NEW_PASSWORD}
        ).status_code
        == 403
    )
    assert browser.delete(f"/api/accounts/{alice.id}").status_code == 403
    assert accounts.get(db_of(app_client), alice.id).role == "user"


def test_disabling_an_account_ends_its_sessions(app_client):
    alice = add_account(app_client, "alice")
    browser = client_for(app_client, alice)
    admin = client_for(app_client, ADMIN)
    assert admin.patch(f"/api/accounts/{alice.id}", json={"disabled": True}).status_code == 200
    assert browser.get("/api/jobs").status_code == 401
    assert login(client_for(app_client, None), "alice").status_code == 401
    assert admin.patch(f"/api/accounts/{alice.id}", json={"disabled": False}).status_code == 200
    assert login(client_for(app_client, None), "alice").status_code == 200


def test_resetting_a_password_ends_the_sessions_of_that_account(app_client):
    alice = add_account(app_client, "alice")
    browser = client_for(app_client, alice)
    admin = client_for(app_client, ADMIN)
    reset = admin.post(f"/api/accounts/{alice.id}/password", json={"password": NEW_PASSWORD})
    assert reset.status_code == 200
    assert browser.get("/api/jobs").status_code == 401
    assert login(client_for(app_client, None), "alice", PASSWORD).status_code == 401
    assert login(client_for(app_client, None), "alice", NEW_PASSWORD).status_code == 200


def test_deleting_an_account_ends_its_sessions(app_client):
    alice = add_account(app_client, "alice")
    browser = client_for(app_client, alice)
    assert client_for(app_client, ADMIN).delete(f"/api/accounts/{alice.id}").status_code == 200
    assert browser.get("/api/jobs").status_code == 401


def test_unknown_accounts_are_a_404(app_client):
    admin = client_for(app_client, ADMIN)
    assert admin.patch("/api/accounts/4242", json={"disabled": True}).status_code == 404
    assert admin.delete("/api/accounts/4242").status_code == 404
    pw = admin.post("/api/accounts/4242/password", json={"password": NEW_PASSWORD})
    assert pw.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"role": 1},
        {"role": None},
        {"disabled": "yes"},
        {"disabled": None},
        {"name": "x"},
        [],
    ],
)
def test_a_bad_update_is_a_422(app_client, body):
    alice = add_account(app_client, "alice")
    response = client_for(app_client, ADMIN).patch(f"/api/accounts/{alice.id}", json=body)
    assert response.status_code == 422


def test_a_mixed_update_that_fails_validation_changes_nothing(app_client):
    """Regression (#76): the role used to be written before `disabled` was checked."""
    alice = add_account(app_client, "alice")
    response = client_for(app_client, ADMIN).patch(
        f"/api/accounts/{alice.id}", json={"role": "admin", "disabled": "yes"}
    )
    assert response.status_code == 422
    stored = accounts.get(db_of(app_client), alice.id)
    assert (stored.role, stored.disabled) == ("user", False)


def test_a_mixed_update_applies_all_fields_at_once(app_client):
    alice = add_account(app_client, "alice")
    response = client_for(app_client, ADMIN).patch(
        f"/api/accounts/{alice.id}",
        json={
            "role": "admin",
            "disabled": False,
            "default_max_height": 720,
            "can_set_resolution": False,
        },
    )
    assert response.status_code == 200
    stored = accounts.get(db_of(app_client), alice.id)
    assert (stored.role, stored.disabled) == ("admin", False)
    assert (stored.default_max_height, stored.can_set_resolution) == (720, False)


def test_a_mixed_update_keeps_the_last_admin_rule(app_client):
    boss = accounts.find(db_of(app_client), ADMIN)[0]
    response = client_for(app_client, ADMIN).patch(
        f"/api/accounts/{boss.id}", json={"role": "user", "disabled": True}
    )
    assert response.status_code == 409
    assert response.json()["kind"] == "last_admin"
    stored = accounts.get(db_of(app_client), boss.id)
    assert (stored.role, stored.disabled) == ("admin", False)


def test_there_is_always_one_active_admin(app_client):
    admin = client_for(app_client, ADMIN)
    boss = accounts.find(db_of(app_client), ADMIN)[0]
    for change_it in (
        lambda: admin.patch(f"/api/accounts/{boss.id}", json={"role": "user"}),
        lambda: admin.patch(f"/api/accounts/{boss.id}", json={"disabled": True}),
        lambda: admin.delete(f"/api/accounts/{boss.id}"),
    ):
        response = change_it()
        assert response.status_code == 409
        assert response.json()["kind"] == "last_admin"
    active_admins = db_of(app_client).query(
        "SELECT COUNT(*) AS n FROM accounts WHERE role = 'admin' AND disabled = 0"
    )[0]["n"]
    assert active_admins == 1
    # with a second admin the first may go
    add_account(app_client, "deputy", "admin")
    assert admin.patch(f"/api/accounts/{boss.id}", json={"role": "user"}).status_code == 200


def test_a_disabled_second_admin_does_not_count(app_client):
    deputy = add_account(app_client, "deputy", "admin")
    accounts.set_disabled(db_of(app_client), deputy.id, True)
    boss = accounts.find(db_of(app_client), ADMIN)[0]
    response = client_for(app_client, ADMIN).delete(f"/api/accounts/{boss.id}")
    assert response.status_code == 409


# --- the brake on guessing ---


def test_five_wrong_passwords_lock_the_name_even_for_the_right_one(app_client):
    codes = [login(client_for(app_client, None), ADMIN, f"wrong password {i}") for i in range(5)]
    assert [r.status_code for r in codes] == [401] * 5
    locked = login(client_for(app_client, None))
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert locked.json()["retry_after"] == int(locked.headers["retry-after"])
    assert "set-cookie" not in locked.headers
    assert db_of(app_client).query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 0


def test_the_lock_is_per_name_and_a_success_starts_the_count_again(app_client):
    add_account(app_client, "alice")
    for i in range(5):
        login(client_for(app_client, None), ADMIN, f"wrong password {i}")
    assert login(client_for(app_client, None), "alice").status_code == 200
    for i in range(4):
        login(client_for(app_client, None), "alice", f"wrong password {i}")
    assert login(client_for(app_client, None), "alice").status_code == 200
    for i in range(4):
        login(client_for(app_client, None), "alice", f"wrong password {i}")
    assert login(client_for(app_client, None), "alice").status_code == 200


def test_unknown_names_are_braked_too_and_the_name_is_not_case_sensitive(app_client):
    for i in range(5):
        login(client_for(app_client, None), "Nobody" if i % 2 else "nobody", "x" * 14)
    assert login(client_for(app_client, None), "NOBODY", "x" * 14).status_code == 429


def test_one_address_trying_many_names_is_braked(app_client):
    app_client.app.state.limiter = LoginLimiter(max_address=3)
    for i in range(3):
        login(client_for(app_client, None), f"nobody{i}", "x" * 14)
    # the right name and password from the same address now also waits
    assert login(client_for(app_client, None)).status_code == 429


def test_a_forwarded_for_header_from_a_stranger_does_not_pick_the_address(app_client):
    app_client.app.state.limiter = LoginLimiter(max_address=3)
    for i in range(3):
        login(
            client_for(app_client, None),
            f"nobody{i}",
            "x" * 14,
            **{"X-Forwarded-For": f"10.0.0.{i}"},
        )
    # the test client's peer is no private address: the header was ignored, the count is shared
    response = login(client_for(app_client, None), **{"X-Forwarded-For": "10.9.9.9"})
    assert response.status_code == 429


def test_the_lock_ends(app_client):
    now = [0.0]
    app_client.app.state.limiter = LoginLimiter(clock=lambda: now[0])
    for i in range(5):
        login(client_for(app_client, None), ADMIN, f"wrong password {i}")
    assert login(client_for(app_client, None)).status_code == 429
    now[0] += 3600
    assert login(client_for(app_client, None)).status_code == 200


# --- no password in any log ---


def test_no_password_or_hash_reaches_a_log_line(app_client, caplog):
    caplog.set_level(logging.DEBUG)
    add_account(app_client, "alice")
    stored = accounts.find(db_of(app_client), "alice")[1]
    secrets_used = [PASSWORD, NEW_PASSWORD, "wrong password!!", "tiny", stored]
    login(client_for(app_client, None), "alice", "wrong password!!")
    login(client_for(app_client, None), "nobody", "wrong password!!")
    browser = client_for(app_client, None)
    login(browser, "alice")
    change(browser, new="tiny")
    change(browser, current="wrong password!!")
    change(browser)
    admin = client_for(app_client, ADMIN)
    admin.post("/api/accounts", json={"username": "carol", "password": PASSWORD})
    admin.post("/api/accounts", json={"username": "dave", "password": "tiny"})
    carol = accounts.find(db_of(app_client), "carol")[0]
    admin.post(f"/api/accounts/{carol.id}/password", json={"password": NEW_PASSWORD})
    for i in range(6):
        login(client_for(app_client, None), ADMIN, f"wrong password {i}")  # until the 429
    logged = caplog.text
    assert "login" in logged  # the log is not empty, or this test would prove nothing
    for secret in secrets_used:
        assert secret not in logged, secret
    assert COOKIE not in logged
    assert accounts.token_hash("x")[:8] not in logged


def test_no_session_token_reaches_a_log_line(app_client, caplog):
    caplog.set_level(logging.DEBUG)
    browser = client_for(app_client, None)
    login(browser)
    token = token_of(browser)
    browser.get("/api/jobs")
    browser.post("/api/auth/logout")
    assert token not in caplog.text
    assert accounts.token_hash(token) not in caplog.text


# --- an installation without any account ---------------------------------------------------------


def test_without_any_account_me_says_that_the_setup_is_missing(tmp_path, capsys):
    with running(tmp_path, APP_ADMIN_PASSWORD_HASH="") as bare:
        response = client_for(bare, None).get("/api/auth/me")
        assert response.status_code == 401
        assert response.json() == {"detail": "login required", "setup": True}
        assert login(client_for(bare, None)).status_code == 401  # and nobody gets in
    assert "make admin-password" in capsys.readouterr().out, "the app logs how to fix it"


def test_with_an_account_me_does_not_say_so(app_client):
    assert client_for(app_client, None).get("/api/auth/me").json() == {"detail": "login required"}


def test_the_page_routes_are_public_so_the_login_can_be_shown(app_client):
    for path in ("/", "/dashboard", "/accounts"):
        response = client_for(app_client, None).get(path)
        assert response.status_code == 200
        assert 'id="login-form"' in response.text
