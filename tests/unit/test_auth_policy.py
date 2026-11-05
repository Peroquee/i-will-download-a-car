"""Who may call which route (#44): every route has an explicit rule, and the rule is enforced."""

from __future__ import annotations

import re

import pytest
from starlette.routing import Mount, Route

from tests.helpers.accounts_app import add_account, client_for, running
from ytp import accounts, auth

pytestmark = [pytest.mark.real_auth, pytest.mark.security]

MISSING = "999999"


def concrete(template: str) -> str:
    return re.sub(r"\{[^}]+\}", MISSING, template)


def routes_of(app) -> set[tuple[str, str]]:
    """(method, route template) of every route the app serves; the static mount counts as `*`."""
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, Mount):
            found.add(("*", route.path))
        elif isinstance(route, Route):
            found.update((m, route.path) for m in route.methods - {"HEAD", "OPTIONS"})
    return found


@pytest.fixture
def app_client(tmp_path):
    with running(tmp_path) as client:
        add_account(client, "alice")
        yield client


def test_every_route_has_an_explicit_policy(app_client):
    missing = sorted(
        (m, p)
        for (m, p) in routes_of(app_client.app)
        if (m, p) not in auth.POLICY and ("*", p) not in auth.POLICY
    )
    assert not missing, f"add these to ytp.auth.POLICY, deliberately: {missing}"


def test_the_policy_names_no_route_that_does_not_exist(app_client):
    served = routes_of(app_client.app)
    stale = sorted(key for key in auth.POLICY if key not in served)
    assert not stale, f"remove these from ytp.auth.POLICY: {stale}"


def test_the_admin_only_routes_are_exactly_the_ones_that_manage_things():
    admin = {key for key, level in auth.POLICY.items() if level == auth.ADMIN}
    assert {path for _, path in admin} == {
        "/api/alerts",
        "/api/alerts/{alert_id}",
        "/api/alerts/ack-all",
        "/api/alerts/{alert_id}/ack",
        "/api/accounts",
        "/api/accounts/{account_id}",
        "/api/accounts/{account_id}/password",
    }


def test_the_public_routes_are_the_page_its_files_and_the_login():
    public = {key for key, level in auth.POLICY.items() if level == auth.PUBLIC}
    assert public == {
        ("GET", "/"),
        ("GET", "/dashboard"),
        ("GET", "/accounts"),
        ("GET", "/health"),
        ("GET", "/api/version"),
        ("POST", "/reload-needed"),
        ("*", "/static"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/me"),
    }


def call(client, method: str, template: str):
    path = "/static/app.js" if template == "/static" else concrete(template)
    if method == "*":
        method = "GET"
    kwargs = {"json": {}} if method in {"POST", "PATCH", "PUT"} else {}
    return client.request(method, path, **kwargs)


def policy_cases():
    for (method, template), level in sorted(auth.POLICY.items()):
        yield pytest.param(method, template, level, id=f"{method} {template}")


@pytest.mark.parametrize(("method", "template", "level"), policy_cases())
def test_an_anonymous_visitor_gets_a_401_unless_the_route_is_public(
    app_client, method, template, level
):
    response = call(client_for(app_client, None), method, template)
    if level == auth.PUBLIC and template != "/api/auth/me":
        assert response.status_code not in (401, 403), response.text
    else:
        assert response.status_code == 401, response.text
        assert response.json() == {"detail": "login required"}


@pytest.mark.parametrize(("method", "template", "level"), policy_cases())
def test_a_user_is_refused_exactly_the_admin_routes(app_client, method, template, level):
    response = call(client_for(app_client, "alice"), method, template)
    if level == auth.ADMIN:
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": "this needs an admin account"}
    else:
        assert response.status_code not in (401, 403), response.text


@pytest.mark.parametrize(("method", "template", "level"), policy_cases())
def test_an_admin_is_never_refused(app_client, method, template, level):
    response = call(client_for(app_client, ADMIN_NAME), method, template)
    assert response.status_code not in (401, 403), response.text


ADMIN_NAME = "boss"


def test_head_is_judged_like_get(app_client):
    # the app answers no HEAD itself (405), but a stranger must not be told that: 401 first
    assert client_for(app_client, None).head("/api/jobs").status_code == 401
    assert client_for(app_client, None).head("/api/alerts").status_code == 401
    assert client_for(app_client, "alice").head("/api/alerts").status_code == 403


def test_a_path_nobody_serves_needs_a_login_before_it_says_404(app_client):
    assert client_for(app_client, None).get("/no/such/page").status_code == 401
    assert client_for(app_client, "alice").get("/no/such/page").status_code == 404


def test_a_wrong_method_on_a_known_route_needs_a_login_first(app_client):
    assert client_for(app_client, None).put("/api/jobs", json={}).status_code == 401
    assert client_for(app_client, "alice").put("/api/jobs", json={}).status_code == 405
    # the least a caller needs is the rule of the strictest method on that path
    assert client_for(app_client, "alice").put("/api/accounts", json={}).status_code == 403


def test_a_route_added_without_a_policy_is_for_admins_only(app_client):
    @app_client.app.get("/api/zz-new-thing")
    def new_thing():
        return {"ok": True}

    assert client_for(app_client, None).get("/api/zz-new-thing").status_code == 401
    assert client_for(app_client, "alice").get("/api/zz-new-thing").status_code == 403
    assert client_for(app_client, ADMIN_NAME).get("/api/zz-new-thing").status_code == 200


@pytest.mark.parametrize(
    "cookie",
    ["", "garbage", "x" * 43, "a" * 500, "ytp_session=ytp_session=x", "\x01\x02"],
)
def test_a_bad_cookie_is_no_login(app_client, cookie):
    other = client_for(app_client, None)
    other.cookies.set(auth.COOKIE, cookie)
    assert other.get("/api/jobs").status_code == 401


def test_a_cookie_of_another_name_is_no_login(app_client):
    token = accounts.new_session(app_client.app.state.db, 1)
    other = client_for(app_client, None)
    other.cookies.set("session", token)
    assert other.get("/api/jobs").status_code == 401


def test_a_session_does_not_work_as_a_header_or_query_parameter(app_client):
    token = accounts.new_session(app_client.app.state.db, 1)
    other = client_for(app_client, None)
    assert other.get("/api/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert other.get(f"/api/jobs?ytp_session={token}").status_code == 401


def test_a_disabled_account_is_locked_out_at_once(app_client):
    alice = client_for(app_client, "alice")
    assert alice.get("/api/jobs").status_code == 200
    who = accounts.find(app_client.app.state.db, "alice")[0]
    accounts.set_disabled(app_client.app.state.db, who.id, True)
    assert alice.get("/api/jobs").status_code == 401


def test_a_disabled_admin_is_not_an_admin(app_client):
    second = add_account(app_client, "deputy", "admin")
    deputy = client_for(app_client, second)
    assert deputy.get("/api/accounts").status_code == 200
    # a disabled admin has no session left, and a session made before could not pass either
    accounts.set_disabled(app_client.app.state.db, second.id, True)
    assert deputy.get("/api/accounts").status_code == 401


def test_a_role_change_applies_to_the_running_session(app_client):
    deputy = add_account(app_client, "deputy", "admin")
    browser = client_for(app_client, deputy)
    assert browser.get("/api/accounts").status_code == 200
    accounts.set_role(app_client.app.state.db, deputy.id, "user")
    assert browser.get("/api/accounts").status_code == 403
