"""The server-wide defaults an admin can change at runtime (issues #79, #80).

* the resolution cap for new videos (`YOUTUBE_MAX_HEIGHT` is the initial value),
* how often subscriptions are looked at, for channels and for podcasts (`SUBSCRIPTION_POLL_MINUTES`
  and `PODCAST_POLL_MINUTES` are the initial values; 0 = paused),
* an optional start time of day ("HH:MM") that anchors those intervals (none at first),
* the check mode, "smart" (around the learned publishing times, the default) or "fixed", and the
  smart mode's numbers (#80): the intensive window from `before` minutes before to `after` minutes
  after a slot's time, a check every `step` minutes inside it, and the slow `base` interval outside.

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
from dataclasses import dataclass
from datetime import tzinfo

from . import poll_schedule, resolution
from .config import Settings
from .db import Database

logger = logging.getLogger(__name__)

KEY_MAX_HEIGHT = "default.max_height"
KEY_MINUTES = {"channel": "default.channel_poll_minutes", "podcast": "default.podcast_poll_minutes"}
KEY_AT = {"channel": "default.channel_poll_at", "podcast": "default.podcast_poll_at"}
KEY_MODE = "default.poll_mode"
KEY_SMART = {
    "smart_before_minutes": "default.smart_before",
    "smart_after_minutes": "default.smart_after",
    "smart_step_minutes": "default.smart_step",
    "smart_base_minutes": "default.smart_base",
}
DEFAULT_MODE = "smart"
# name: (default, lowest, highest). The step is also the shortest gap between two checks of a feed.
SMART_LIMITS = {
    "smart_before_minutes": (10, 0, 60),
    "smart_after_minutes": (60, 5, 240),
    "smart_step_minutes": (5, 5, 60),
    "smart_base_minutes": (360, 60, 1440),
}

# The names of the API (PATCH /api/settings/defaults) and what each one means.
FIELDS = (
    "max_height",
    "subscription_poll_minutes",
    "subscription_poll_at",
    "podcast_poll_minutes",
    "podcast_poll_at",
    "poll_mode",
    *SMART_LIMITS,
)
_KIND_OF = {
    "subscription_poll_minutes": "channel",
    "subscription_poll_at": "channel",
    "podcast_poll_minutes": "podcast",
    "podcast_poll_at": "podcast",
}


@dataclass(frozen=True)
class SmartSettings:
    before: int = 10  # minutes before a slot's time the intensive window begins
    after: int = 60  # minutes after it the window ends
    step: int = 5  # a check every this many minutes inside the window
    base: int = 360  # the slow interval outside it


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

    def _mode(self, stored: dict[str, str]) -> str:
        raw = stored.get(KEY_MODE)
        return raw if raw in poll_schedule.MODES else DEFAULT_MODE

    def _smart(self, name: str, stored: dict[str, str]) -> int:
        default, low, high = SMART_LIMITS[name]
        raw = stored.get(KEY_SMART[name])
        if raw is not None and raw.isdigit() and low <= int(raw) <= high:
            return int(raw)
        return default

    def schedule(self, kind: str) -> poll_schedule.Schedule:
        """The default interval, start time and mode for channels or podcasts."""
        stored = self._stored()
        return poll_schedule.Schedule(
            self._minutes(kind, stored), self._at(kind, stored), self._mode(stored)
        )

    def smart_settings(self) -> SmartSettings:
        """The smart mode's window, step and base interval (#80)."""
        stored = self._stored()
        return SmartSettings(*(self._smart(name, stored) for name in SMART_LIMITS))

    def snapshot(self) -> dict[str, object]:
        """What the API returns: the values in force, which of them an admin set, the limits."""
        stored = self._stored()
        return {
            "max_height": self._max_height(stored),
            "subscription_poll_minutes": self._minutes("channel", stored),
            "subscription_poll_at": self._at("channel", stored),
            "podcast_poll_minutes": self._minutes("podcast", stored),
            "podcast_poll_at": self._at("podcast", stored),
            "poll_mode": self._mode(stored),
            **{name: self._smart(name, stored) for name in SMART_LIMITS},
            "custom": {
                "max_height": KEY_MAX_HEIGHT in stored,
                "subscription_poll_minutes": KEY_MINUTES["channel"] in stored,
                "subscription_poll_at": KEY_AT["channel"] in stored,
                "podcast_poll_minutes": KEY_MINUTES["podcast"] in stored,
                "podcast_poll_at": KEY_AT["podcast"] in stored,
                "poll_mode": KEY_MODE in stored,
                **{name: KEY_SMART[name] in stored for name in SMART_LIMITS},
            },
            "timezone": poll_schedule.zone_name(self.zone),
            "limits": {
                "choices": list(resolution.MAX_HEIGHTS),
                "min_minutes": dict(poll_schedule.MIN_MINUTES),
                "max_minutes": poll_schedule.MAX_MINUTES,
                "modes": list(poll_schedule.MODES),
                "smart": {
                    name: {"min": low, "max": high} for name, (_, low, high) in SMART_LIMITS.items()
                },
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
            if field == "poll_mode":
                if value is not None and value not in poll_schedule.MODES:
                    raise Invalid("poll_mode must be " + " or ".join(poll_schedule.MODES))
                plan[field] = (KEY_MODE, value)
                continue
            if field in SMART_LIMITS:
                _, low, high = SMART_LIMITS[field]
                valid = (
                    isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
                )
                if value is not None and not valid:
                    raise Invalid(f"{field} must be a whole number from {low} to {high}")
                plan[field] = (KEY_SMART[field], None if value is None else str(value))
                continue
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
