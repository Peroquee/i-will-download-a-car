"""Accounts, sessions and who owns what (issue #44).

Plain functions over `ytp.db.Database`; every value goes in as a query parameter.

* **Accounts** have a name (3 to 64 characters, case-insensitive and unique), a password hash
  (`ytp.passwords`) and a role: `admin` or `user`. A disabled account cannot log in and its
  sessions stop working at once. There is always at least one active admin: the last one cannot
  be deleted, disabled or made a plain user (`LastAdmin`).
* **Sessions:** the cookie value is 32 random bytes; only its SHA-256 is stored. A session expires
  after `IDLE_DAYS` without use or `MAX_DAYS` after the login, whichever is first; a new one is
  made at every login (no session id is ever reused: session fixation). A password change or reset
  ends the other sessions of that account.
* **Ownership:** a job has owners, a channel or podcast subscription has followers. One video is
  one job however many people asked for it, so asking again adds an owner to the existing row.
  `visible_job_ids` / `follows` are what the routes ask. Rows that exist before the first account
  go to the first admin (`adopt_orphans`).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import passwords, resolution
from .db import Database

ROLES = ("admin", "user")
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
IDLE_DAYS = 14
MAX_DAYS = 30
TOUCH_SECONDS = 300  # last_seen is written at most this often per session


class AccountError(Exception):
    """An account change that cannot be done. `kind` is stable, the text is English."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class LastAdmin(AccountError):
    def __init__(self) -> None:
        super().__init__("last_admin", "there must always be one active admin")


@dataclass(frozen=True)
class Account:
    id: int
    username: str
    role: str
    disabled: bool
    created_at: str
    last_login: str | None
    # The resolution the account gets when a download sends none; None = the server default (#65).
    default_max_height: int | None = None
    # Whether an admin lets this account choose resolutions; admins always may (#65).
    can_set_resolution: bool = True

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" and not self.disabled

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Account:
        return cls(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            disabled=bool(row["disabled"]),
            created_at=row["created_at"],
            last_login=row["last_login"],
            default_max_height=row["default_max_height"],
            can_set_resolution=bool(row["can_set_resolution"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "disabled": self.disabled,
            "created_at": self.created_at,
            "last_login": self.last_login,
            "default_max_height": self.default_max_height,
            "can_set_resolution": self.can_set_resolution,
        }


def now_text(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(UTC)
    return (
        moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    )


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ------------------------------------------------------------------------------------ accounts


def check_username(name: str) -> str | None:
    if not isinstance(name, str) or not USERNAME.fullmatch(name):
        return "the name needs 3 to 64 characters: letters, digits, . _ - (not first)"
    return None


def create(db: Database, username: str, password: str, role: str = "user") -> Account:
    problem = check_username(username) or passwords.check_strength(password)
    if problem:
        raise AccountError("invalid", problem)
    if role not in ROLES:
        raise AccountError("invalid", "the role must be admin or user")
    hashed = passwords.hash_password(password)
    return create_with_hash(db, username, hashed, role)


def create_with_hash(db: Database, username: str, hashed: str, role: str) -> Account:
    try:
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO accounts (username, password_hash, role) VALUES (?, ?, ?)",
                (username, hashed, role),
            )
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except sqlite3.IntegrityError:
        raise AccountError("exists", "an account with this name exists already") from None
    return Account.from_row(row)


def get(db: Database, account_id: int) -> Account | None:
    rows = db.query("SELECT * FROM accounts WHERE id = ?", (account_id,))
    return Account.from_row(rows[0]) if rows else None


def find(db: Database, username: str) -> tuple[Account, str] | None:
    """(account, password hash) for a name, or None."""
    rows = db.query("SELECT * FROM accounts WHERE username = ?", (username,))
    return (Account.from_row(rows[0]), rows[0]["password_hash"]) if rows else None


def list_all(db: Database) -> list[Account]:
    return [
        Account.from_row(r)
        for r in db.query("SELECT * FROM accounts ORDER BY username COLLATE NOCASE")
    ]


def any_exists(db: Database) -> bool:
    """Is there at least one account? One row, no account loaded (#76)."""
    return bool(db.query("SELECT 1 FROM accounts LIMIT 1"))


def count_active_admins(db: Database) -> int:
    return db.query("SELECT COUNT(*) AS n FROM accounts WHERE role = 'admin' AND disabled = 0")[0][
        "n"
    ]


def _would_remove_last_admin(conn: sqlite3.Connection, account_id: int) -> bool:
    others = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE role = 'admin' AND disabled = 0 AND id <> ?",
        (account_id,),
    ).fetchone()[0]
    return others == 0


UNCHANGED = object()  # "leave this field alone" for update()
_UNCHANGED = UNCHANGED  # the old name, kept for the callers that spelled it with an underscore


def update(
    db: Database,
    account_id: int,
    *,
    role: str | object = UNCHANGED,
    disabled: bool | object = UNCHANGED,
    default_max_height: int | None | object = UNCHANGED,
    can_set_resolution: bool | object = UNCHANGED,
) -> Account:
    """Change several fields of an account in ONE transaction (#76).

    Every value is validated before anything is written, so a request can never be applied
    halfway. `UNCHANGED` leaves a field alone; `default_max_height=None` clears the personal
    default. The last-active-admin rule is judged on the result that would be written.
    """
    if role is not UNCHANGED and (not isinstance(role, str) or role not in ROLES):
        raise AccountError("invalid", "the role must be admin or user")
    if disabled is not UNCHANGED and not isinstance(disabled, bool):
        raise AccountError("invalid", "disabled must be true or false")
    if (
        default_max_height is not UNCHANGED
        and default_max_height is not None
        and not resolution.valid(default_max_height)
    ):
        raise AccountError("invalid", resolution.DETAIL)
    if can_set_resolution is not UNCHANGED and not isinstance(can_set_resolution, bool):
        raise AccountError("invalid", "can_set_resolution must be true or false")
    updates: list[str] = []
    values: list[Any] = []
    if role is not UNCHANGED:
        updates.append("role = ?")
        values.append(role)
    if disabled is not UNCHANGED:
        updates.append("disabled = ?")
        values.append(int(disabled))
    if default_max_height is not UNCHANGED:
        updates.append("default_max_height = ?")
        values.append(default_max_height)
    if can_set_resolution is not UNCHANGED:
        updates.append("can_set_resolution = ?")
        values.append(int(can_set_resolution))
    if not updates:
        raise AccountError("invalid", "nothing to change")
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountError("not_found", "no such account")
        new_role = row["role"] if role is UNCHANGED else role
        new_disabled = bool(row["disabled"]) if disabled is UNCHANGED else bool(disabled)
        active_now = row["role"] == "admin" and not row["disabled"]
        active_after = new_role == "admin" and not new_disabled
        if active_now and not active_after and _would_remove_last_admin(conn, account_id):
            raise LastAdmin
        conn.execute(
            f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?", (*values, account_id)
        )
        if disabled is True:
            conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return Account.from_row(row)


def set_role(db: Database, account_id: int, role: str) -> Account:
    return update(db, account_id, role=role)


def set_disabled(db: Database, account_id: int, disabled: bool) -> Account:
    return update(db, account_id, disabled=disabled)


def set_resolution_settings(
    db: Database,
    account_id: int,
    *,
    default_max_height: int | None | object = UNCHANGED,
    can_set_resolution: bool | object = UNCHANGED,
) -> Account:
    """Change one or both resolution settings of an account (#65). `UNCHANGED` leaves a setting
    alone; `default_max_height=None` clears the personal default."""
    return update(
        db,
        account_id,
        default_max_height=default_max_height,
        can_set_resolution=can_set_resolution,
    )


def set_default_max_height(db: Database, account_id: int, max_height: int | None) -> Account:
    """The account's own default resolution (it may only do this with the admin's right; the
    API checks that)."""
    return set_resolution_settings(db, account_id, default_max_height=max_height)


def set_password(
    db: Database, account_id: int, password: str, *, keep_session: str | None = None
) -> None:
    """New password; every other session of the account ends (`keep_session`: a token hash)."""
    problem = passwords.check_strength(password)
    if problem:
        raise AccountError("invalid", problem)
    hashed = passwords.hash_password(password)
    set_password_hash(db, account_id, hashed, keep_session=keep_session)


def set_password_hash(
    db: Database, account_id: int, hashed: str, *, keep_session: str | None = None
) -> None:
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE id = ?", (hashed, account_id)
        )
        if cursor.rowcount != 1:
            raise AccountError("not_found", "no such account")
        conn.execute(
            "DELETE FROM sessions WHERE account_id = ? AND token_hash IS NOT ?",
            (account_id, keep_session),
        )


def delete(db: Database, account_id: int) -> None:
    """Remove an account. Its follows end (a channel nobody follows any more is removed too);
    jobs it started stay, for the admin."""
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountError("not_found", "no such account")
        if (
            row["role"] == "admin"
            and not row["disabled"]
            and _would_remove_last_admin(conn, account_id)
        ):
            raise LastAdmin
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))  # cascades
        conn.execute(
            "DELETE FROM subscriptions "
            "WHERE id NOT IN (SELECT subscription_id FROM subscription_followers)"
        )
        conn.execute(
            "DELETE FROM podcast_subscriptions "
            "WHERE id NOT IN (SELECT subscription_id FROM podcast_followers)"
        )


def record_login(db: Database, account_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE accounts SET last_login = ? WHERE id = ?", (now_text(), account_id))


# ------------------------------------------------------------------------------------ sessions


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(db: Database, account_id: int, now: datetime | None = None) -> str:
    """Start a session; returns the cookie value (only its hash is stored)."""
    now = now or datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, account_id, created_at, last_seen, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                token_hash(token),
                account_id,
                now_text(now),
                now_text(now),
                now_text(now + timedelta(days=MAX_DAYS)),
            ),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_text(now),))
    return token


def session_account(db: Database, token: str, now: datetime | None = None) -> Account | None:
    """The account of a valid session, else None. Slides the idle timeout (rarely written)."""
    now = now or datetime.now(UTC)
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        return None
    digest = token_hash(token)
    rows = db.query(
        "SELECT s.last_seen AS last_seen, s.expires_at AS expires_at, a.* FROM sessions s "
        "JOIN accounts a ON a.id = s.account_id WHERE s.token_hash = ?",
        (digest,),
    )
    if not rows:
        return None
    row = rows[0]
    if row["disabled"]:
        return None
    last_seen = _parse(row["last_seen"])
    if _parse(row["expires_at"]) < now or last_seen + timedelta(days=IDLE_DAYS) < now:
        end_session(db, token)
        return None
    if (now - last_seen).total_seconds() > TOUCH_SECONDS:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now_text(now), digest)
            )
    return Account.from_row(row)


def end_session(db: Database, token: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def end_all_sessions(db: Database, account_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))


# ------------------------------------------------------------------------------------ ownership


def add_job_owner(db: Database, job_id: int, account_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO job_owners (job_id, account_id) VALUES (?, ?)",
            (job_id, account_id),
        )


def owns_job(db: Database, job_id: int, account_id: int) -> bool:
    return bool(
        db.query(
            "SELECT 1 FROM job_owners WHERE job_id = ? AND account_id = ?", (job_id, account_id)
        )
    )


def follow(db: Database, kind: str, subscription_id: int, account_id: int) -> bool:
    """Make the account a follower; True if it was not one before. kind: channel | podcast."""
    table = FOLLOW_TABLES[kind]
    with db.transaction() as conn:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO {table} (subscription_id, account_id) VALUES (?, ?)",
            (subscription_id, account_id),
        )
        return cursor.rowcount == 1


def follows(db: Database, kind: str, subscription_id: int, account_id: int) -> bool:
    table = FOLLOW_TABLES[kind]
    return bool(
        db.query(
            f"SELECT 1 FROM {table} WHERE subscription_id = ? AND account_id = ?",
            (subscription_id, account_id),
        )
    )


def followers(db: Database, kind: str, subscription_id: int) -> list[int]:
    table = FOLLOW_TABLES[kind]
    rows = db.query(
        f"SELECT account_id FROM {table} WHERE subscription_id = ? ORDER BY account_id",
        (subscription_id,),
    )
    return [r["account_id"] for r in rows]


def followed_ids(db: Database, kind: str, account_id: int) -> set[int]:
    table = FOLLOW_TABLES[kind]
    rows = db.query(
        f"SELECT subscription_id FROM {table} WHERE account_id = ?",
        (account_id,),
    )
    return {r["subscription_id"] for r in rows}


def follower_names_by_subscription(db: Database, kind: str) -> dict[int, list[str]]:
    """The follower names of EVERY subscription of a kind, in one query (#76).

    The list routes used to ask `followers` and `follower_names` once per row; this is the
    same answer for the whole page.
    """
    table = FOLLOW_TABLES[kind]
    rows = db.query(
        f"SELECT f.subscription_id AS subscription_id, a.username AS name FROM {table} f "
        "JOIN accounts a ON a.id = f.account_id "
        "ORDER BY f.subscription_id, a.username COLLATE NOCASE"
    )
    names: dict[int, list[str]] = {}
    for row in rows:
        names.setdefault(row["subscription_id"], []).append(row["name"])
    return names


def follower_names(db: Database, kind: str, subscription_id: int) -> list[str]:
    table = FOLLOW_TABLES[kind]
    rows = db.query(
        f"SELECT a.username AS name FROM {table} f JOIN accounts a ON a.id = f.account_id "
        "WHERE f.subscription_id = ? ORDER BY a.username COLLATE NOCASE",
        (subscription_id,),
    )
    return [r["name"] for r in rows]


def unfollow(db: Database, kind: str, subscription_id: int, account_id: int) -> str:
    """Remove a follower. Returns "ended" if nobody follows any more (the subscription is deleted),
    "kept" if others do, "none" if the account was not following."""
    table = FOLLOW_TABLES[kind]
    parent = PARENT_TABLES[kind]
    with db.transaction() as conn:
        removed = conn.execute(
            f"DELETE FROM {table} WHERE subscription_id = ? AND account_id = ?",
            (subscription_id, account_id),
        ).rowcount
        if not removed:
            return "none"
        left = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()[0]
        if left == 0:
            conn.execute(f"DELETE FROM {parent} WHERE id = ?", (subscription_id,))
            return "ended"
        return "kept"


FOLLOW_TABLES = {"channel": "subscription_followers", "podcast": "podcast_followers"}
PARENT_TABLES = {"channel": "subscriptions", "podcast": "podcast_subscriptions"}


def adopt_orphans(db: Database, admin_id: int) -> dict[str, int]:
    """Give everything that has no owner or follower yet to this admin (the rows from before the
    first account existed). Safe to repeat."""
    with db.transaction() as conn:
        jobs = conn.execute(
            "INSERT INTO job_owners (job_id, account_id) "
            "SELECT id, ? FROM jobs WHERE id NOT IN (SELECT job_id FROM job_owners)",
            (admin_id,),
        ).rowcount
        channels = conn.execute(
            "INSERT INTO subscription_followers (subscription_id, account_id) "
            "SELECT id, ? FROM subscriptions "
            "WHERE id NOT IN (SELECT subscription_id FROM subscription_followers)",
            (admin_id,),
        ).rowcount
        podcasts = conn.execute(
            "INSERT INTO podcast_followers (subscription_id, account_id) "
            "SELECT id, ? FROM podcast_subscriptions "
            "WHERE id NOT IN (SELECT subscription_id FROM podcast_followers)",
            (admin_id,),
        ).rowcount
    return {"jobs": jobs, "channels": channels, "podcasts": podcasts}


# ------------------------------------------------------------------------------------ bootstrap

KEY_APPLIED = "auth.admin_hash_applied"


def bootstrap_admin(db: Database, username: str, hashed: str) -> str:
    """Make sure the admin named in `.env` exists, with the password hash `.env` holds.

    * no such account: it is created as an active admin, and everything that has no owner yet
      (jobs and subscriptions from before the accounts) is given to it;
    * the account exists and `.env` holds a NEW hash (not the one applied last time): its password
      becomes that hash, it is made an active admin again and its sessions end. This is how a lost
      admin password is recovered: set a new one with `make admin-password`, restart the app;
    * otherwise nothing changes (the password can be changed in the app and stays changed).

    Returns "created", "reset" or "unchanged".
    """
    from .scheduler import get_state, set_state

    marker = hashlib.sha256(hashed.encode()).hexdigest()
    existing = find(db, username)
    if existing is None:
        account = create_with_hash(db, username, hashed, "admin")
        adopt_orphans(db, account.id)
        set_state(db, KEY_APPLIED, marker)
        return "created"
    account, _ = existing
    adopt_orphans(db, account.id)
    if get_state(db, KEY_APPLIED) == marker:
        return "unchanged"
    with db.transaction() as conn:
        conn.execute("UPDATE accounts SET role = 'admin', disabled = 0 WHERE id = ?", (account.id,))
    set_password_hash(db, account.id, hashed)
    set_state(db, KEY_APPLIED, marker)
    return "reset"
