"""The alert log (issue #38): what a person should know about, stored in the app's database.

`add` is the one way in, for the monitor (`ytp.monitor`) and for later features (the automatic
update, #42). Rules that make it safe to show:

* **No addresses and no secrets.** Every text and every parameter is passed through `redact`,
  which replaces anything that looks like an IPv4 or IPv6 address. The callers do not hand in
  secrets in the first place (no magnet links, no tokens, no passwords).
* **Texts are English fallbacks.** The page translates by `kind` and shows `params`; `title` and
  `detail` are what an unknown kind (or an older page) falls back to.
* **Recording twice is harmless** when a `key` is given (`UNIQUE dedupe_key`).
* **The log stays small:** at most `MAX_ALERTS` rows, none older than `MAX_AGE_DAYS`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import Database

SEVERITIES = ("info", "warning", "error")
MAX_ALERTS = 500
MAX_AGE_DAYS = 90
MAX_TEXT = 500
MAX_PARAM = 300
KIND = re.compile(r"[a-z][a-z0-9_]{0,39}")

# Anything that looks like an address: dotted quads, and colon-separated hex groups (IPv6).
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HEX = r"[0-9a-f]{1,4}"
_IPV6 = re.compile(
    rf"(?<![\w:])(?:(?:{_HEX}:){{7}}{_HEX}"  # full form
    rf"|(?:{_HEX}:){{0,6}}[0-9a-f]{{0,4}}::(?:{_HEX}:){{0,6}}[0-9a-f]{{0,4}})(?![\w:])",  # with ::
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Replace addresses with a placeholder."""
    return _IPV6.sub("[address]", _IPV4.sub("[address]", text))


def _clean(value: object, limit: int) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return redact(" ".join(text.split()))[:limit]


@dataclass(frozen=True)
class Alert:
    id: int
    created_at: str
    kind: str
    severity: str
    title: str
    detail: str
    params: dict[str, Any]
    acknowledged: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Alert:
        try:
            params = json.loads(row["params"])
        except ValueError:
            params = {}
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            kind=row["kind"],
            severity=row["severity"],
            title=row["title"],
            detail=row["detail"],
            params=params if isinstance(params, dict) else {},
            acknowledged=bool(row["acknowledged"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "time": self.created_at,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "params": self.params,
            "acknowledged": self.acknowledged,
        }


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if not isinstance(key, str) or not KIND.fullmatch(key):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            cleaned[key] = value
        else:
            cleaned[key] = _clean(value, MAX_PARAM)
    return cleaned


def insert(
    conn: sqlite3.Connection,
    kind: str,
    severity: str,
    title: str,
    detail: str = "",
    params: dict[str, Any] | None = None,
    *,
    key: str | None = None,
) -> sqlite3.Row | None:
    """The insert itself, on a connection (inside the caller's transaction, `row_factory` Row).

    Shared by `add` and by host scripts that write to the app's database directly (the automatic
    update, #42). Returns the new row, or None if `key` was recorded before.
    """
    if not KIND.fullmatch(kind):
        raise ValueError(f"bad alert kind {kind!r}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    cursor = conn.execute(
        "INSERT INTO alerts (kind, severity, title, detail, params, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (dedupe_key) DO NOTHING",
        (
            kind,
            severity,
            _clean(title, MAX_TEXT),
            _clean(detail, MAX_TEXT),
            json.dumps(_clean_params(params), ensure_ascii=False),
            key,
        ),
    )
    if cursor.rowcount != 1:
        return None
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (cursor.lastrowid,)).fetchone()
    conn.execute(
        "DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts ORDER BY id DESC LIMIT ?) "
        "OR created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (MAX_ALERTS, f"-{MAX_AGE_DAYS} days"),
    )
    return row


def add(
    db: Database,
    kind: str,
    severity: str,
    title: str,
    detail: str = "",
    params: dict[str, Any] | None = None,
    *,
    key: str | None = None,
) -> Alert | None:
    """Record an alert. Returns it, or None if `key` was recorded before."""
    with db.transaction() as conn:
        row = insert(conn, kind, severity, title, detail, params, key=key)
    return Alert.from_row(row) if row is not None else None


def list_alerts(db: Database, *, limit: int = 50, unread_only: bool = False) -> list[Alert]:
    """Newest first."""
    where = "WHERE acknowledged = 0 " if unread_only else ""
    rows = db.query(f"SELECT * FROM alerts {where}ORDER BY id DESC LIMIT ?", (limit,))
    return [Alert.from_row(row) for row in rows]


def get(db: Database, alert_id: int) -> Alert | None:
    rows = db.query("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    return Alert.from_row(rows[0]) if rows else None


def unread_count(db: Database) -> int:
    return db.query("SELECT COUNT(*) AS n FROM alerts WHERE acknowledged = 0")[0]["n"]


def acknowledge(db: Database, alert_id: int) -> bool:
    """Mark one alert as read. False if there is no such alert; True also if it already was."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE alerts SET acknowledged = 1, "
            "acknowledged_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ? AND acknowledged = 0",
            (alert_id,),
        )
        return conn.execute("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)).fetchone() is not None


def acknowledge_all(db: Database) -> int:
    """Mark everything as read; returns how many were unread."""
    with db.transaction() as conn:
        return conn.execute(
            "UPDATE alerts SET acknowledged = 1, "
            "acknowledged_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE acknowledged = 0"
        ).rowcount


def record_vpn_event(db: Database, condition: str, paused: bool, reason: str) -> None:
    """One row in the VPN history (a change of condition)."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO vpn_events (condition, paused, reason) VALUES (?, ?, ?)",
            (condition, int(paused), _clean(reason, 200)),
        )
        conn.execute(
            "DELETE FROM vpn_events WHERE id NOT IN "
            "(SELECT id FROM vpn_events ORDER BY id DESC LIMIT 500)"
        )


def vpn_history(db: Database, limit: int = 30) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM vpn_events ORDER BY id DESC LIMIT ?", (limit,))
    return [
        {
            "time": r["created_at"],
            "condition": r["condition"],
            "paused": bool(r["paused"]),
            "reason": r["reason"],
        }
        for r in rows
    ]
