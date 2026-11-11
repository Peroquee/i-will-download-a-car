"""What the smart mode knows about one subscription, for the page (issue #80).

The learner and the scheduler live in `smart_schedule` and `poll_schedule`; this only turns their
answers into a small JSON object the subscriptions list and the Settings window show:

    {"mode": "smart", "regular": true, "confidence": 0.93, "events": 14,
     "slots": [{"weekday": 1, "minute": 1020}, {"weekday": 4, "minute": 1020}],
     "next_window": {"start": "...Z", "expected": "...Z", "active": false} | null}

`weekday` is 0 = Monday .. 6 = Sunday and `minute` the minute of the day, both in the server's time
zone (the one the Settings window names); the page turns them into "Tue and Fri 5 pm" in its own
language. `slots` is empty and `regular` false while the pattern is unknown (the interval is used).
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from . import publish_history
from .db import Database
from .poll_schedule import Schedule
from .server_defaults import SmartSettings
from .smart_schedule import learn, windows
from .timeutil import now_text

MAX_SLOTS = 14  # a daily uploader has 7; two a day would be 14


def describe(
    db: Database,
    kind: str,
    subscription_id: int,
    schedule: Schedule,
    settings: SmartSettings,
    zone: tzinfo,
    now: datetime,
) -> dict[str, Any]:
    times = publish_history.times(db, kind, subscription_id)
    pattern = learn(times, zone, now)
    info: dict[str, Any] = {
        "mode": schedule.mode,
        "regular": pattern.regular,
        "confidence": pattern.confidence,
        "events": pattern.events,
        "slots": [
            {"weekday": slot.weekday, "minute": slot.minute} for slot in pattern.slots[:MAX_SLOTS]
        ],
        "next_window": None,
    }
    if pattern.regular:
        for window in windows(pattern, now, zone, settings.before, settings.after):
            if window.end >= now:
                info["next_window"] = {
                    "start": now_text(window.start),
                    "expected": now_text(window.expected),
                    "active": window.start <= now,
                }
                break
    return info
