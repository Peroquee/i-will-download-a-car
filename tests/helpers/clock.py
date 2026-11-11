"""Clocks the tests move by hand (issue #77): one float clock and one calendar clock."""

from __future__ import annotations

from datetime import datetime, timedelta


class Clock:
    """A monotonic clock: `clock.advance(seconds)` moves it, `clock()` reads it."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DatetimeClock:
    """A wall clock for code that takes datetimes: `clock.advance(timedelta(...))`."""

    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, delta: timedelta) -> None:
        self.t += delta
