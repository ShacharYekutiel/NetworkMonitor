"""[2b-1] Clock used by maintenance and alerting (UTC, plus a test offset)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

MAX_OFFSET_S = 31536000


class Clock:
    """`now()` is the wall clock (UTC) shifted by `offset_s` (test mode only).

    `changed` is set whenever the offset changes; it is replaced by a fresh event at that
    moment, so a waiter takes `clock.changed` right before it waits."""

    def __init__(self, offset_s: float = 0.0) -> None:
        self.offset_s = float(offset_s)
        self.changed = asyncio.Event()

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset_s)

    def set_offset(self, offset_s: float) -> None:
        self.offset_s = float(offset_s)
        self.notify()

    def notify(self) -> None:
        old, self.changed = self.changed, asyncio.Event()
        old.set()


class FakeClock(Clock):
    """A clock that only moves when told to (unit tests)."""

    def __init__(self, start: datetime | None = None) -> None:
        super().__init__()
        self._now = start or datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now + timedelta(seconds=self.offset_s)

    def set(self, when: datetime) -> None:
        self._now = when
        self.offset_s = 0.0
        self.notify()

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self.notify()
