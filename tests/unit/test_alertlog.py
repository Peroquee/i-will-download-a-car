"""The alert log: storing, redacting, acknowledging, pruning (issue #38)."""

from __future__ import annotations

import json

import pytest

from ytp import alertlog
from ytp.db import Database


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database.open(tmp_path / "app.db")
    yield database
    database.close()


# --- the log itself -------------------------------------------------------------------------


def test_an_alert_is_stored_and_listed_newest_first(db):
    alertlog.add(db, "job_failed", "error", "First", "d1", {"job_id": 1})
    alertlog.add(db, "disk_low", "warning", "Second", "d2")
    got = alertlog.list_alerts(db)
    assert [a.title for a in got] == ["Second", "First"]
    assert got[1].params == {"job_id": 1}
    assert got[0].acknowledged is False
    assert alertlog.unread_count(db) == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("VPN 10.1.2.3 is down", "VPN [address] is down"),
        ("from 84.150.20.30 to x", "from [address] to x"),
        ("2003:abcd:1200::1 is the prefix", "[address] is the prefix"),
        ("full 2a02:1:2:3:4:5:6:7 form", "full [address] form"),
        ("loopback ::1 too", "loopback [address] too"),
        ("at 12:30:45 sharp", "at 12:30:45 sharp"),
        ("ratio 1:2, version 1.2.3", "ratio 1:2, version 1.2.3"),
    ],
)
def test_addresses_are_redacted_and_ordinary_text_is_not(text, expected):
    assert alertlog.redact(text) == expected


def test_no_address_reaches_the_database_from_title_detail_or_params(db):
    alert = alertlog.add(
        db,
        "job_failed",
        "error",
        "failed 10.0.0.5",
        "the host 192.168.178.1 refused",
        {"reason": "connect to 2003:abcd::1 failed", "n": 3, "ok": True, "none": None},
    )
    stored = json.dumps(alert.as_dict())
    for fragment in ("10.0.0.5", "192.168.178.1", "2003:abcd"):
        assert fragment not in stored
    assert alert.params["n"] == 3
    assert alert.params["ok"] is True
    raw = db.query("SELECT title, detail, params FROM alerts")[0]
    assert "10.0.0.5" not in "".join(map(str, tuple(raw)))


def test_control_characters_and_long_texts_are_cleaned(db):
    alert = alertlog.add(db, "x_y", "info", "a\x00b\nc" + "z" * 900, "", {"reason": "q" * 900})
    assert "\x00" not in alert.title
    assert "\n" not in alert.title
    assert len(alert.title) <= alertlog.MAX_TEXT
    assert len(alert.params["reason"]) <= alertlog.MAX_PARAM


def test_bad_kinds_severities_and_param_names_are_refused_or_dropped(db):
    with pytest.raises(ValueError, match="kind"):
        alertlog.add(db, "Bad Kind", "info", "t")
    with pytest.raises(ValueError, match="severity"):
        alertlog.add(db, "ok_kind", "loud", "t")
    alert = alertlog.add(db, "ok_kind", "info", "t", params={"Bad Name": 1, "good": 2})
    assert alert.params == {"good": 2}


def test_the_same_key_is_recorded_once(db):
    assert alertlog.add(db, "job_failed", "error", "t", key="job:1") is not None
    assert alertlog.add(db, "job_failed", "error", "t", key="job:1") is None
    assert alertlog.add(db, "job_failed", "error", "t") is not None, "no key: always recorded"
    assert len(alertlog.list_alerts(db)) == 2


def test_acknowledging_marks_read_and_is_idempotent(db):
    a = alertlog.add(db, "disk_low", "warning", "t")
    b = alertlog.add(db, "disk_low", "warning", "t")
    assert alertlog.acknowledge(db, a.id) is True
    assert alertlog.acknowledge(db, a.id) is True
    assert alertlog.acknowledge(db, 999) is False
    assert alertlog.get(db, a.id).acknowledged is True
    assert alertlog.get(db, b.id).acknowledged is False
    assert [x.id for x in alertlog.list_alerts(db, unread_only=True)] == [b.id]
    assert alertlog.acknowledge_all(db) == 1
    assert alertlog.unread_count(db) == 0


def test_the_log_is_pruned_by_count_and_by_age(db, monkeypatch):
    monkeypatch.setattr(alertlog, "MAX_ALERTS", 5)
    for i in range(8):
        alertlog.add(db, "disk_low", "warning", f"t{i}")
    assert [a.title for a in alertlog.list_alerts(db, limit=100)] == [
        f"t{i}" for i in (7, 6, 5, 4, 3)
    ]
    with db.transaction() as conn:
        conn.execute("UPDATE alerts SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = 4")
    alertlog.add(db, "disk_low", "warning", "fresh")
    assert 4 not in [a.id for a in alertlog.list_alerts(db, limit=100)]
