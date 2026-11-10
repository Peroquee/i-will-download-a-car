"""The app's timestamps: one writer and one reader (issue #75).

Every time the app stores is UTC ISO text; SQLite has no date type. Before this module the
format was written in four places (`accounts.now_text`, `scheduler._now_iso`, `storage._iso`,
two `strftime` calls in the reconciler) and parsed in five. `now_text` writes milliseconds and a
`Z` suffix - the format SQLite's own `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` produces, so the
text sorts in time order. `parse_time` reads any ISO 8601 time the app stores or receives.

Python 3.11 (the project's minimum, see pyproject.toml) reads the `Z` suffix itself, so the old
`.replace("Z", "+00:00")` workaround is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_text(moment: datetime | None = None) -> str:
    """The current UTC time (or `moment`) as the app stores it: '2026-11-01T05:00:00.123Z'."""
    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_time(text: str) -> datetime:
    """An ISO 8601 time the app stores ('...Z' or an offset); ValueError if it is none."""
    return datetime.fromisoformat(text)


def parse_time_or_none(value: object) -> datetime | None:
    """`parse_time` for untrusted input (a feed entry, the watchdog's JSON): None if no time.

    A time without an offset counts as UTC, as everywhere in the app; surrounding whitespace is
    tolerated (feeds occasionally pad their dates)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
