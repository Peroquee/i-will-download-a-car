"""The server-wide defaults an admin can change at runtime (issue #79).

* the resolution cap for new videos (`YOUTUBE_MAX_HEIGHT` is the initial value),
* how often subscriptions are looked at, for channels and for podcasts (`SUBSCRIPTION_POLL_MINUTES`
  and `PODCAST_POLL_MINUTES` are the initial values; 0 = paused),
* an optional start time of day ("HH:MM") that anchors those intervals (none at first).

They live in `app_state` (key/value), so nothing changes for an install until an admin saves one:
a missing key means "the value from the environment". Every read goes to the database, so a new
value takes effect without a restart. A stored value that is not valid any more (edited by hand)
is ignored and the environment value is used.

Subscriptions have their own values too (`subscriptions.max_height`, `.poll_minutes`, `.poll_at`);
`None` there means "follow these defaults".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import tzinfo

from . import poll_schedule, resolution
from .config import Settings
from .db import Database

logger = logging.getLogger(__name__)

KEY_MAX_HEIGHT = "default.max_height"
KEY_MINUTES = {"channel": "default.channel_poll_minutes", "podcast": "default.podcast_poll_minutes"}
KEY_AT = {"channel": "default.channel_poll_at", "podcast": "default.podcast_poll_at"}

# The names of the API (PATCH /api/settings/defaults) and what each one means.
FIELDS = (
    "max_height",
    "subscription_poll_minutes",
    "subscription_poll_at",
    "podcast_poll_minutes",
    "podcast_poll_at",
)
_KIND_OF = {
    "subscription_poll_minutes": "channel",
    "subscription_poll_at": "channel",
    "podcast_poll_minutes": "podcast",
    "podcast_poll_at": "podcast",
}


class Invalid(ValueError):
    """A value the API refuses; the message says what is allowed."""


class ServerDefaults:
    def __init__(
        self,
        database: Callable[[], Database | None],
        settings: Settings,
        zone: tzinfo | None = None,
    ):
        self._database = database  # a getter: the app opens (or reopens) its database late
        self._settings = settings
        self.zone = zone or poll_schedule.local_zone()

    # -- reading ----------------------------------------------------------------------------

    def _stored(self) -> dict[str, str]:
        db = self._database()
        if db is None:
            return {}
        rows = db.query("SELECT key, value FROM app_state WHERE key LIKE 'default.%'")
        return {row["key"]: row["value"] for row in rows}

    def _env_minutes(self, kind: str) -> int:
        s = self._settings
        return s.subscription_poll_minutes if kind == "channel" else s.podcast_poll_minutes

    def _minutes(self, kind: str, stored: dict[str, str]) -> int:
        raw = stored.get(KEY_MINUTES[kind])
        if raw is not None and raw.isdigit() and poll_schedule.minutes_valid(kind, int(raw)):
            return int(raw)
        return self._env_minutes(kind)

    def _at(self, kind: str, stored: dict[str, str]) -> str | None:
        raw = stored.get(KEY_AT[kind])
        return raw if raw is not None and poll_schedule.at_valid(raw) else None

    def _max_height(self, stored: dict[str, str]) -> int:
        raw = stored.get(KEY_MAX_HEIGHT)
        if raw is not None and raw.isdigit() and resolution.valid(int(raw)):
            return int(raw)
        return self._settings.youtube_max_height

    def max_height(self) -> int:
        return self._max_height(self._stored())

    def schedule(self, kind: str) -> poll_schedule.Schedule:
        """The default interval and start time for channels or podcasts."""
        stored = self._stored()
        return poll_schedule.Schedule(self._minutes(kind, stored), self._at(kind, stored))

    def snapshot(self) -> dict[str, object]:
        """What the API returns: the values in force, which of them an admin set, the limits."""
        stored = self._stored()
        return {
            "max_height": self._max_height(stored),
            "subscription_poll_minutes": self._minutes("channel", stored),
            "subscription_poll_at": self._at("channel", stored),
            "podcast_poll_minutes": self._minutes("podcast", stored),
            "podcast_poll_at": self._at("podcast", stored),
            "custom": {
                "max_height": KEY_MAX_HEIGHT in stored,
                "subscription_poll_minutes": KEY_MINUTES["channel"] in stored,
                "subscription_poll_at": KEY_AT["channel"] in stored,
                "podcast_poll_minutes": KEY_MINUTES["podcast"] in stored,
                "podcast_poll_at": KEY_AT["podcast"] in stored,
            },
            "timezone": poll_schedule.zone_name(self.zone),
            "limits": {
                "choices": list(resolution.MAX_HEIGHTS),
                "min_minutes": dict(poll_schedule.MIN_MINUTES),
                "max_minutes": poll_schedule.MAX_MINUTES,
            },
        }

    # -- writing ----------------------------------------------------------------------------

    @staticmethod
    def check(changes: dict[str, object]) -> dict[str, tuple[str, str | None]]:
        """Validate a PATCH body. Returns {key: (app_state key, new text or None to remove)}.

        `None` resets a field to the environment's value. A start time of "" counts as none."""
        unknown = sorted(set(changes) - set(FIELDS))
        if unknown or not changes:
            raise Invalid("send one or more of " + ", ".join(FIELDS))
        plan: dict[str, tuple[str, str | None]] = {}
        for field, value in changes.items():
            if field == "max_height":
                if value is not None and not resolution.valid(value):
                    raise Invalid(resolution.DETAIL)
                plan[field] = (KEY_MAX_HEIGHT, None if value is None else str(value))
                continue
            kind = _KIND_OF[field]
            if field.endswith("_minutes"):
                if value is not None and not poll_schedule.minutes_valid(kind, value):
                    raise Invalid(poll_schedule.RANGE_HELP[kind].replace("poll_minutes", field))
                plan[field] = (KEY_MINUTES[kind], None if value is None else str(value))
            else:
                if value in (None, ""):
                    plan[field] = (KEY_AT[kind], None)
                elif not poll_schedule.at_valid(value):
                    raise Invalid(poll_schedule.AT_HELP.replace("poll_at", field))
                else:
                    plan[field] = (KEY_AT[kind], str(value))
        return plan

    def update(self, changes: dict[str, object]) -> dict[str, object]:
        """Save the given fields (all or nothing) and return the new `snapshot()`."""
        plan = self.check(changes)
        db = self._database()
        if db is None:
            raise RuntimeError("the database is not available")
        with db.transaction() as conn:
            for key, text in plan.values():
                if text is None:
                    conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
                else:
                    conn.execute(
                        "INSERT INTO app_state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                        (key, text),
                    )
        logger.info("server defaults changed", extra={"fields": sorted(changes)})
        return self.snapshot()
