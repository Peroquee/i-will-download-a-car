"""The one timestamp writer and reader (issue #75): one format, one parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import ytp
from ytp import timeutil


def test_now_text_writes_utc_with_milliseconds_and_a_z_suffix():
    moment = datetime(2026, 11, 1, 5, 0, 0, 123456, tzinfo=UTC)
    assert timeutil.now_text(moment) == "2026-11-01T05:00:00.123Z"
    two_hours_ahead = moment.astimezone(timezone(timedelta(hours=2)))
    assert timeutil.now_text(two_hours_ahead) == "2026-11-01T05:00:00.123Z", "always UTC"
    assert timeutil.now_text().endswith("Z")


def test_parse_time_reads_what_now_text_writes():
    moment = datetime(2026, 11, 1, 5, 0, 0, 123000, tzinfo=UTC)
    assert timeutil.parse_time(timeutil.now_text(moment)) == moment
    assert timeutil.parse_time("2026-11-01T05:00:00Z") == datetime(2026, 11, 1, 5, tzinfo=UTC)
    assert timeutil.parse_time("2026-11-01T06:00:00+01:00") == datetime(2026, 11, 1, 5, tzinfo=UTC)
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        timeutil.parse_time("not a time")


@pytest.mark.parametrize("value", [None, 42, "", "  ", "yesterday", [], {}, "2026-13-01T00:00:00Z"])
def test_parse_time_or_none_refuses_what_is_not_a_time(value):
    assert timeutil.parse_time_or_none(value) is None


def test_parse_time_or_none_treats_a_missing_offset_as_utc():
    assert timeutil.parse_time_or_none(" 2026-11-01T05:00:00 ") == datetime(
        2026, 11, 1, 5, tzinfo=UTC
    )
    assert timeutil.parse_time_or_none("2026-11-01T06:00:00+01:00") == datetime(
        2026, 11, 1, 5, tzinfo=UTC
    )


def test_no_other_module_parses_timestamps_or_keeps_the_z_workaround():
    """#75: the five parsers and four writers are gone; a new copy fails this test.

    `date.fromisoformat` (a date, not a timestamp) is not counted."""
    app = Path(ytp.__file__).parent
    offenders = {}
    for path in sorted(app.glob("*.py")):
        if path.name == "timeutil.py":
            continue
        source = path.read_text(encoding="utf-8")
        hits = []
        if "datetime.fromisoformat(" in source:
            hits.append("own timestamp parser")
        if '.replace("Z", "+00:00")' in source:
            hits.append("Z workaround")
        if 'strftime("%Y-%m-%dT%H:%M' in source:
            hits.append("own timestamp writer")
        if hits:
            offenders[path.name] = hits
    assert offenders == {}
