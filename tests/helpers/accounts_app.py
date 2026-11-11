"""A running app with real logins, for the tests of accounts and permissions (#44).

Use with `@pytest.mark.real_auth` (otherwise every request counts as the test admin, see
tests/conftest.py). `running()` starts the app with the admin "boss"; `add_account()` makes more
accounts straight in the database (fast hash, so a test does not wait for scrypt), and
`client_for()` gives a separate browser (own cookie jar) that is logged in as an account.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from ytp import accounts, passwords
from ytp.accounts import Account
from ytp.auth import COOKIE
from ytp.config import Settings
from ytp.main import create_app

from .app import ENV

FAST_N = 2**10  # the cost is in the stored hash: a test hash verifies quickly
PASSWORD = "a long test password"  # a test value, not a secret
ADMIN = "boss"


def fast_hash(password: str = PASSWORD) -> str:
    return passwords.hash_password(password, n=FAST_N)


@contextlib.contextmanager
def running(tmp_path: Path, **env: str) -> Iterator[TestClient]:
    """The app with the bootstrap admin "boss" (password PASSWORD). The client has no login."""
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    values = {
        **ENV,
        "DATA_ROOT": str(tmp_path),
        "CONFIG_DIR": str(config),
        "APP_ADMIN_USER": ADMIN,
        "APP_ADMIN_PASSWORD_HASH": fast_hash(),
        **env,
    }
    client = TestClient(create_app(Settings.from_env(values)))
    client.__enter__()
    try:
        yield client
    finally:
        client.__exit__(None, None, None)


def add_account(
    client: TestClient, name: str, role: str = "user", password: str = PASSWORD
) -> Account:
    return accounts.create_with_hash(client.app.state.db, name, fast_hash(password), role)


def client_for(client: TestClient, account: Account | str | None) -> TestClient:
    """A second browser on the same app, logged in (a session made in the database) as the
    account, or, for None, with no cookie at all."""
    other = TestClient(client.app)
    if account is not None:
        who = (
            accounts.find(client.app.state.db, account)[0] if isinstance(account, str) else account
        )
        other.cookies.set(COOKIE, accounts.new_session(client.app.state.db, who.id))
    return other


def db_of(client: TestClient):
    """The app's database behind a TestClient (issue #77)."""
    return client.app.state.db


def people(app_client: TestClient) -> SimpleNamespace:
    """alice and bob, each with a browser, and the admin (issue #77)."""
    alice = add_account(app_client, "alice")
    bob = add_account(app_client, "bob")
    return SimpleNamespace(
        alice=client_for(app_client, alice),
        bob=client_for(app_client, bob),
        admin=client_for(app_client, ADMIN),
        alice_id=alice.id,
        bob_id=bob.id,
    )
