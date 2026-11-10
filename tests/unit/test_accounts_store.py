"""The building blocks of accounts (#44): passwords, the brake, sessions, ownership, bootstrap."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.helpers.accounts_app import fast_hash
from ytp import accounts, auth, jobs, passwords, podcast_subscriptions, subscriptions
from ytp.accounts import AccountError
from ytp.auth import TRUSTED_PROXY_SECONDS, LoginLimiter, TrustedProxies, client_address
from ytp.config import ConfigError, Settings
from ytp.db import Database
from ytp.scheduler import get_state

pytestmark = pytest.mark.security

PASSWORD = "a long test password"  # a test value, not a secret
CID = "UCSMOQeBJ2RAnuFungnQOxLg"
CID2 = "UC" + "b" * 22


@pytest.fixture
def db(tmp_path) -> Iterator[Database]:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


# --- passwords ---


def test_a_hash_verifies_only_its_own_password():
    stored = fast_hash(PASSWORD)
    assert passwords.verify_password(PASSWORD, stored)
    assert not passwords.verify_password(PASSWORD + "x", stored)
    assert not passwords.verify_password("", stored)


def test_a_hash_is_one_dollar_free_line_with_a_fresh_salt_each_time():
    one, two = fast_hash(PASSWORD), fast_hash(PASSWORD)
    assert one != two
    for stored in (one, two):
        assert "$" not in stored
        assert "\n" not in stored
        assert PASSWORD not in stored
        assert stored.startswith("scrypt:")
        assert passwords.is_hash(stored)


def test_the_default_cost_is_the_documented_one():
    stored = passwords.hash_password(PASSWORD)
    assert stored.startswith(f"scrypt:{2**15}:8:1:")
    assert not passwords.needs_rehash(stored)
    assert passwords.needs_rehash(fast_hash(PASSWORD))


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "changeme",
        "plain password",
        "scrypt:1:2:3",
        "scrypt:32768:8:1:!!!:???",
        "scrypt:99999999999:8:1:AAAAAAAAAAAAAAAAAAAAAA:" + "A" * 43,  # would allocate gigabytes
        "scrypt:3:8:1:AAAAAAAAAAAAAAAAAAAAAA:" + "A" * 43,  # not a power of two
        "bcrypt:32768:8:1:AAAAAAAAAAAAAAAAAAAAAA:" + "A" * 43,
        "scrypt:32768:8:1:AAAA:" + "A" * 43,  # salt too short
        "scrypt:32768:8:1:AAAAAAAAAAAAAAAAAAAAAA:AAAA",  # key too short
    ],
)
def test_a_missing_or_broken_hash_never_matches_and_never_raises(stored):
    assert passwords.verify_password(PASSWORD, stored) is False
    assert passwords.verify_password("", stored) is False
    assert not passwords.is_hash(stored)


def test_a_too_long_password_is_not_accepted_even_if_it_is_the_stored_one():
    stored = fast_hash(PASSWORD)
    assert passwords.verify_password("x" * 500, stored) is False
    assert passwords.verify_password(None, stored) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("password", "ok"),
    [
        ("x" * 11, False),
        ("x" * 12, True),
        ("x" * 200, True),
        ("x" * 201, False),
        ("a\x00bbbbbbbbbbbbb", False),
        ("ünïcödé-pässwörd", True),
        (None, False),
        (12345678901234, False),
    ],
)
def test_the_strength_rules(password, ok):
    assert (passwords.check_strength(password) is None) is ok


def test_a_weak_password_cannot_be_hashed():
    with pytest.raises(ValueError, match="at least"):
        passwords.hash_password("short")


# --- the brake ---


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_limiter_allows_four_failures_and_locks_at_five():
    clock = Clock()
    limiter = LoginLimiter(clock=clock)
    keys = limiter.keys("alice", "10.0.0.1")
    for _ in range(4):
        limiter.failed(*keys)
        assert limiter.wait(*keys) == 0
    limiter.failed(*keys)
    assert limiter.wait(*keys) == pytest.approx(30)


def test_the_lock_doubles_with_every_further_failure_up_to_a_ceiling():
    clock = Clock()
    limiter = LoginLimiter(clock=clock, window=10_000)
    keys = limiter.keys("alice", "10.0.0.1")
    waits = []
    for _ in range(12):
        limiter.failed(*keys)
        waits.append(limiter.wait(*keys))
    assert waits[4:9] == [30, 60, 120, 240, 480]
    assert max(waits) == 900


def test_the_lock_runs_out_and_old_failures_are_forgotten():
    clock = Clock()
    limiter = LoginLimiter(clock=clock)
    keys = limiter.keys("alice", "10.0.0.1")
    for _ in range(5):
        limiter.failed(*keys)
    clock.now += 31
    assert limiter.wait(*keys) == 0
    clock.now += 900
    limiter.failed(*keys)  # the old five are out of the window: this is the first again
    assert limiter.wait(*keys) == 0


def test_a_success_clears_only_the_user_and_the_names_are_normalised():
    limiter = LoginLimiter(clock=Clock())
    user, address = limiter.keys(" Alice ", "10.0.0.1")
    assert user == limiter.keys("alice", "10.0.0.2")[0]
    for _ in range(5):
        limiter.failed(user, address)
    limiter.succeeded(user)
    assert limiter.wait(user) == 0
    assert limiter.wait(address) == 0  # 5 of 30


def test_the_address_limit_is_higher_than_the_name_limit():
    limiter = LoginLimiter(clock=Clock())
    address = limiter.keys("x", "10.0.0.1")[1]
    for _ in range(29):
        limiter.failed(address)
    assert limiter.wait(address) == 0
    limiter.failed(address)
    assert limiter.wait(address) > 0


def test_a_flood_of_made_up_names_does_not_grow_the_memory_without_end():
    clock = Clock()
    limiter = LoginLimiter(clock=clock, max_keys=50)
    for i in range(500):
        limiter.failed(*limiter.keys(f"name{i}", "10.0.0.1"))
    assert len(limiter._failures) <= 52


def scope(peer, forwarded=None):
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return {"client": (peer, 5555), "headers": headers}


@pytest.mark.parametrize(
    ("peer", "forwarded", "trusted", "expected"),
    [
        ("172.18.0.5", "192.168.1.20", {"172.18.0.5"}, "192.168.1.20"),  # the proxy: believed
        ("172.18.0.5", "6.6.6.6, 192.168.1.20", {"172.18.0.5"}, "192.168.1.20"),  # its own last
        ("127.0.0.1", "192.168.1.20", {"127.0.0.1"}, "192.168.1.20"),
        ("172.18.0.5", "not an address", {"172.18.0.5"}, "172.18.0.5"),
        ("172.18.0.5", None, {"172.18.0.5"}, "172.18.0.5"),
        # Regression (#76): another container on the compose bridge is private too, but is not
        # the proxy and must not choose the rate-limit key.
        ("172.18.0.9", "192.168.1.20", {"172.18.0.5"}, "172.18.0.9"),
        ("172.18.0.5", "192.168.1.20", set(), "172.18.0.5"),  # no proxy configured
        ("8.8.8.8", "192.168.1.20", {"172.18.0.5"}, "8.8.8.8"),  # a stranger: ignored
    ],
)
def test_the_forwarded_address_is_believed_only_from_the_trusted_proxy(
    peer, forwarded, trusted, expected
):
    assert client_address(scope(peer, forwarded), trusted) == expected


def test_no_client_at_all_still_gives_a_key():
    assert client_address({"headers": []}) == "unknown"


def test_the_trusted_proxy_is_resolved_once_and_cached():
    calls: list[str] = []
    clock = Clock()
    trusted = TrustedProxies(
        "proxy",
        resolver=lambda name: calls.append(name) or ["172.18.0.10"],
        clock=clock,
    )
    assert "172.18.0.10" in trusted
    assert "172.18.0.11" not in trusted
    clock.now += TRUSTED_PROXY_SECONDS - 1
    assert "172.18.0.10" in trusted
    assert calls == ["proxy"], "the answer is cached"
    clock.now += 2
    assert "172.18.0.10" in trusted
    assert calls == ["proxy", "proxy"], "and looked up again after the TTL"


def test_a_trusted_proxy_name_that_does_not_resolve_trusts_nobody():
    def broken(name):
        raise OSError("no such host")

    trusted = TrustedProxies("proxy", resolver=broken)
    assert "172.18.0.10" not in trusted


def test_no_trusted_proxy_name_means_no_header_is_believed():
    trusted = TrustedProxies("", resolver=lambda name: ["172.18.0.10"])
    assert "172.18.0.10" not in trusted
    assert client_address(scope("172.18.0.10", "192.168.1.20"), trusted) == "172.18.0.10"


def test_a_peer_that_is_not_an_address_never_matches():
    trusted = TrustedProxies("proxy", resolver=lambda name: ["172.18.0.10"])
    assert "testclient" not in trusted
    assert client_address(scope("testclient", "192.168.1.20"), trusted) == "testclient"


# --- sessions ---


@pytest.fixture
def alice(db):
    return accounts.create_with_hash(db, "alice", fast_hash(), "user")


def test_a_session_is_valid_until_it_expires(db, alice):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    token = accounts.new_session(db, alice.id, start)
    assert accounts.session_account(db, token, start + timedelta(days=1)).id == alice.id
    assert accounts.session_account(db, token, start + timedelta(days=29, hours=23)) is None


def test_an_unused_session_ends_after_the_idle_time(db, alice):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    token = accounts.new_session(db, alice.id, start)
    assert accounts.session_account(db, token, start + timedelta(days=15)) is None
    assert accounts.session_account(db, token, start + timedelta(days=1)) is None  # and it is gone


def test_use_keeps_a_session_alive_but_not_beyond_the_maximum(db, alice):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    token = accounts.new_session(db, alice.id, start)
    for day in range(10, 31, 10):
        assert accounts.session_account(db, token, start + timedelta(days=day - 1)) is not None
    assert accounts.session_account(db, token, start + timedelta(days=31)) is None


@pytest.mark.parametrize("token", ["", "short", "x" * 200, None, 5, "a" * 43])
def test_a_token_that_was_never_issued_is_no_session(db, alice, token):
    assert accounts.session_account(db, token) is None


def test_an_expired_session_is_cleaned_out_when_a_new_one_starts(db, alice):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    accounts.new_session(db, alice.id, start)
    accounts.new_session(db, alice.id, start + timedelta(days=40))
    assert db.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1


def test_ending_all_sessions_of_one_account_leaves_the_others(db, alice):
    bob = accounts.create_with_hash(db, "bob", fast_hash(), "user")
    mine, theirs = accounts.new_session(db, alice.id), accounts.new_session(db, bob.id)
    accounts.end_all_sessions(db, alice.id)
    assert accounts.session_account(db, mine) is None
    assert accounts.session_account(db, theirs).id == bob.id


# --- account rules ---


@pytest.mark.parametrize(
    "name", ["ab", "x" * 65, " alice", "a lice", "-alice", "ali;ce", "al/ice", ""]
)
def test_bad_names_are_refused(db, name):
    with pytest.raises(AccountError) as error:
        accounts.create(db, name, PASSWORD)
    assert error.value.kind == "invalid"


def test_names_are_unique_regardless_of_case(db, alice):
    with pytest.raises(AccountError) as error:
        accounts.create_with_hash(db, "ALICE", fast_hash(), "user")
    assert error.value.kind == "exists"


def test_a_new_account_is_a_user_unless_told_otherwise_and_roles_are_checked(db):
    with pytest.raises(AccountError):
        accounts.create(db, "carol", PASSWORD, "superuser")
    assert accounts.create(db, "carol", PASSWORD).role == "user"


# --- who follows what ---


@pytest.fixture
def two(db):
    return (
        accounts.create_with_hash(db, "alice", fast_hash(), "user"),
        accounts.create_with_hash(db, "bob", fast_hash(), "user"),
    )


def test_two_followers_share_one_subscription_and_it_ends_with_the_last(db, two):
    alice, bob = two
    sub, created = subscriptions.add(db, CID, "Blender")
    assert created
    assert accounts.follow(db, "channel", sub.id, alice.id) is True
    assert accounts.follow(db, "channel", sub.id, alice.id) is False
    accounts.follow(db, "channel", sub.id, bob.id)
    assert accounts.followers(db, "channel", sub.id) == [alice.id, bob.id]
    assert accounts.follower_names(db, "channel", sub.id) == ["alice", "bob"]
    assert accounts.unfollow(db, "channel", sub.id, alice.id) == "kept"
    assert subscriptions.get(db, sub.id) is not None
    assert accounts.unfollow(db, "channel", sub.id, alice.id) == "none"
    assert accounts.unfollow(db, "channel", sub.id, bob.id) == "ended"
    assert subscriptions.get(db, sub.id) is None


def test_podcasts_work_the_same_way(db, two):
    alice, bob = two
    sub, _ = podcast_subscriptions.add(db, "https://example.org/feed.xml", "Show")
    accounts.follow(db, "podcast", sub.id, alice.id)
    accounts.follow(db, "podcast", sub.id, bob.id)
    assert accounts.followed_ids(db, "podcast", bob.id) == {sub.id}
    assert accounts.unfollow(db, "podcast", sub.id, alice.id) == "kept"
    assert accounts.unfollow(db, "podcast", sub.id, bob.id) == "ended"
    assert podcast_subscriptions.get(db, sub.id) is None


def test_following_is_per_kind(db, two):
    alice, _ = two
    channel, _ = subscriptions.add(db, CID, "Blender")
    accounts.follow(db, "channel", channel.id, alice.id)
    assert not accounts.follows(db, "podcast", channel.id, alice.id)


def test_deleting_an_account_removes_its_follows_and_channels_nobody_follows(db, two):
    alice, bob = two
    shared, _ = subscriptions.add(db, CID, "Shared")
    private, _ = subscriptions.add(db, CID2, "Private")
    accounts.follow(db, "channel", shared.id, alice.id)
    accounts.follow(db, "channel", shared.id, bob.id)
    accounts.follow(db, "channel", private.id, alice.id)
    accounts.delete(db, alice.id)
    assert subscriptions.get(db, shared.id) is not None
    assert subscriptions.get(db, private.id) is None
    assert accounts.followers(db, "channel", shared.id) == [bob.id]


def test_deleting_an_account_keeps_the_jobs_it_started(db, two):
    alice, _ = two
    job, _ = jobs.submit_job(db, "youtube", "aaaaaaaaaaa", "{}")
    accounts.add_job_owner(db, job.id, alice.id)
    accounts.delete(db, alice.id)
    assert jobs.get_job(db, job.id) is not None
    assert not db.query("SELECT 1 FROM job_owners WHERE job_id = ?", (job.id,))


def test_follower_names_can_be_read_for_every_subscription_at_once(db, two):
    alice, bob = two
    first, _ = subscriptions.add(db, CID, "Blender")
    second, _ = subscriptions.add(db, CID2, "Other")
    accounts.follow(db, "channel", first.id, alice.id)
    accounts.follow(db, "channel", first.id, bob.id)
    accounts.follow(db, "channel", second.id, bob.id)
    assert accounts.follower_names_by_subscription(db, "channel") == {
        first.id: ["alice", "bob"],
        second.id: ["bob"],
    }


def test_any_exists_answers_without_loading_the_accounts(db):
    assert accounts.any_exists(db) is False
    accounts.create_with_hash(db, "alice", fast_hash(), "user")
    assert accounts.any_exists(db) is True


# --- one transaction for a partial update (#76) ---


def test_update_applies_every_field_at_once(db, two):
    alice, _ = two
    changed = accounts.update(
        db,
        alice.id,
        role="admin",
        disabled=True,
        default_max_height=720,
        can_set_resolution=False,
    )
    assert (changed.role, changed.disabled) == ("admin", True)
    assert (changed.default_max_height, changed.can_set_resolution) == (720, False)


def test_update_leaves_fields_alone_that_are_unchanged(db, two):
    alice, _ = two
    accounts.update(db, alice.id, default_max_height=1080)
    changed = accounts.update(db, alice.id, disabled=True)
    assert changed.default_max_height == 1080
    assert changed.disabled is True


def test_update_validates_everything_before_writing(db, two):
    alice, _ = two
    with pytest.raises(AccountError) as error:
        accounts.update(db, alice.id, role="admin", disabled="yes")
    assert error.value.kind == "invalid"
    stored = accounts.get(db, alice.id)
    assert (stored.role, stored.disabled) == ("user", False), "nothing was written"


def test_update_keeps_the_last_active_admin_rule(db):
    boss = accounts.create_with_hash(db, "boss", fast_hash(), "admin")
    with pytest.raises(AccountError) as error:
        accounts.update(db, boss.id, role="user", disabled=True)
    assert error.value.kind == "last_admin"
    stored = accounts.get(db, boss.id)
    assert (stored.role, stored.disabled) == ("admin", False)


def test_update_ends_the_sessions_of_a_disabled_account(db, two):
    alice, _ = two
    token = accounts.new_session(db, alice.id)
    accounts.update(db, alice.id, disabled=True)
    assert accounts.session_account(db, token) is None


def test_job_owners_can_be_several_and_adding_twice_is_harmless(db, two):
    alice, bob = two
    job, _ = jobs.submit_job(db, "youtube", "aaaaaaaaaaa", "{}")
    accounts.add_job_owner(db, job.id, alice.id)
    accounts.add_job_owner(db, job.id, alice.id)
    accounts.add_job_owner(db, job.id, bob.id)
    assert accounts.owns_job(db, job.id, alice.id)
    assert accounts.owns_job(db, job.id, bob.id)
    assert [j.id for j in jobs.list_jobs(db, owner_id=alice.id)] == [job.id]


# --- the first admin ---


def old_data(db):
    sub, _ = subscriptions.add(db, CID, "Old channel")
    pod, _ = podcast_subscriptions.add(db, "https://example.org/feed.xml", "Old show")
    job, _ = jobs.submit_job(db, "youtube", "aaaaaaaaaaa", "{}")
    return sub, pod, job


def test_the_first_admin_is_created_and_adopts_what_was_there_before(db):
    sub, pod, job = old_data(db)
    assert accounts.bootstrap_admin(db, "boss", fast_hash()) == "created"
    boss = accounts.find(db, "boss")[0]
    assert boss.is_admin
    assert accounts.followers(db, "channel", sub.id) == [boss.id]
    assert accounts.followers(db, "podcast", pod.id) == [boss.id]
    assert accounts.owns_job(db, job.id, boss.id)


def test_a_second_start_with_the_same_hash_changes_nothing_even_after_a_password_change(db):
    hashed = fast_hash()
    accounts.bootstrap_admin(db, "boss", hashed)
    boss = accounts.find(db, "boss")[0]
    accounts.set_password(db, boss.id, "changed in the app!")
    session = accounts.new_session(db, boss.id)
    assert accounts.bootstrap_admin(db, "boss", hashed) == "unchanged"
    assert passwords.verify_password("changed in the app!", accounts.find(db, "boss")[1])
    assert accounts.session_account(db, session) is not None


def test_a_new_hash_in_env_resets_the_password_and_the_sessions_and_repairs_the_account(db):
    accounts.bootstrap_admin(db, "boss", fast_hash("the first password"))
    boss = accounts.find(db, "boss")[0]
    token = accounts.new_session(db, boss.id)
    accounts.create_with_hash(db, "deputy", fast_hash(), "admin")
    accounts.set_role(db, boss.id, "user")
    accounts.set_disabled(db, boss.id, True)
    assert accounts.bootstrap_admin(db, "boss", fast_hash("the new password")) == "reset"
    again = accounts.find(db, "boss")
    assert again[0].is_admin
    assert passwords.verify_password("the new password", again[1])
    assert not passwords.verify_password("the first password", again[1])
    assert accounts.session_account(db, token) is None


def test_the_marker_is_a_hash_of_the_hash_not_the_hash_itself(db):
    hashed = fast_hash()
    accounts.bootstrap_admin(db, "boss", hashed)
    marker = get_state(db, accounts.KEY_APPLIED)
    assert marker
    assert marker != hashed
    assert hashed not in marker


def test_bootstrap_adopts_leftovers_for_an_existing_admin_too(db):
    hashed = fast_hash()
    accounts.bootstrap_admin(db, "boss", hashed)
    sub, _, job = old_data(db)
    assert accounts.bootstrap_admin(db, "boss", hashed) == "unchanged"
    boss = accounts.find(db, "boss")[0]
    assert accounts.followers(db, "channel", sub.id) == [boss.id]
    assert accounts.owns_job(db, job.id, boss.id)


def test_adopting_never_takes_what_somebody_already_owns(db, two):
    alice, _ = two
    sub, _, job = old_data(db)
    accounts.follow(db, "channel", sub.id, alice.id)
    accounts.add_job_owner(db, job.id, alice.id)
    boss = accounts.create_with_hash(db, "boss", fast_hash(), "admin")
    assert accounts.adopt_orphans(db, boss.id) == {"jobs": 0, "channels": 0, "podcasts": 1}
    assert accounts.followers(db, "channel", sub.id) == [alice.id]


# --- settings ---

ENV = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hb",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}


def test_the_admin_settings_default_to_no_bootstrap():
    settings = Settings.from_env(ENV)
    assert settings.admin_user == "admin"
    assert settings.admin_password_hash == ""
    assert settings.summary()["admin_bootstrap"] is False


def test_the_templates_placeholder_means_not_set():
    assert (
        Settings.from_env({**ENV, "APP_ADMIN_PASSWORD_HASH": "changeme"}).admin_password_hash == ""
    )


def test_a_real_hash_is_taken_and_never_shown():
    hashed = fast_hash()
    settings = Settings.from_env(
        {**ENV, "APP_ADMIN_USER": "boss", "APP_ADMIN_PASSWORD_HASH": hashed}
    )
    assert settings.admin_password_hash == hashed
    assert settings.summary()["admin_bootstrap"] is True
    assert hashed not in repr(settings)
    assert hashed not in str(settings.summary())


@pytest.mark.parametrize(
    "hashed", ["plain-password-typed-by-mistake", "$2a$14$abc", "scrypt:1:2:3"]
)
def test_a_value_that_is_no_hash_is_refused_at_start(hashed):
    with pytest.raises(ConfigError) as error:
        Settings.from_env({**ENV, "APP_ADMIN_PASSWORD_HASH": hashed})
    assert hashed not in str(error.value)


def test_a_bad_admin_name_is_refused_at_start():
    with pytest.raises(ConfigError):
        Settings.from_env({**ENV, "APP_ADMIN_USER": "no good"})


# --- the test hook must not exist in production ---


def test_the_test_account_is_not_a_global_in_the_production_modules():
    """`create_app(test_account=...)` is the only way in (#76): no module under app/ keeps a
    switch that could make every request an admin."""
    root = Path(__file__).resolve().parents[2]
    code = "from ytp import auth; print(hasattr(auth, 'TEST_ACCOUNT'))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root / "app",
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.strip() == "False"
    for path in (root / "app" / "ytp").rglob("*.py"):
        assert "TEST_ACCOUNT" not in path.read_text(encoding="utf-8"), path
    assert auth.COOKIE == "ytp_session"
