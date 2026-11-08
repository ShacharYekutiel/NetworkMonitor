"""[2b-3] Ingestion limits (architecture section 12.2.2) and the token buckets."""

from __future__ import annotations

import time
from collections import OrderedDict

TRAP_PER_SOURCE_RATE, TRAP_PER_SOURCE_BURST = 50.0, 200.0
TRAP_GLOBAL_RATE, TRAP_GLOBAL_BURST = 500.0, 2000.0
SYSLOG_PER_SOURCE_RATE, SYSLOG_PER_SOURCE_BURST = 200.0, 1000.0
SYSLOG_GLOBAL_RATE, SYSLOG_GLOBAL_BURST = 2000.0, 10000.0

RAW_QUEUE = 20000
WRITE_QUEUE = 10000
MAX_DATAGRAM = 65535
MAX_TCP_FRAME = 65536
MAX_VARBINDS = 128
MAX_VARBIND_VALUE = 1024
MAX_SUMMARY = 1000
MAX_SYSLOG_MESSAGE = 8192
TCP_MAX_CONNECTIONS = 512
TCP_MAX_PER_SOURCE = 32
TCP_IDLE_TIMEOUT_S = 900.0
TCP_QUEUE_WAIT_S = 2.0
TRACKED_SOURCES = 4096

WRITE_BATCH = 1000
WRITE_INTERVAL_S = 0.5
LIVE_INTERVAL_S = 1.0
LIVE_MAX = 200
FORWARD_QUEUE = 10000
MAX_FORWARD_TARGETS = 4
DROP_EVENTS_PER_HOUR = 10

DROP_REASONS = (
    "rate_limited",
    "queue_full",
    "too_large",
    "parse_error",
    "ignored_source",
    "unknown_source",
    "bad_community",
    "v3_auth",
    "unsupported",
    "db_error",
)


class TokenBucket:
    __slots__ = ("rate", "burst", "tokens", "stamp")

    def __init__(self, rate: float, burst: float, now: float | None = None) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.stamp = time.monotonic() if now is None else now

    def take(self, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        elapsed = t - self.stamp
        if elapsed > 0:
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.stamp = t
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Per-source buckets (LRU of `TRACKED_SOURCES`) plus one global bucket. A message must
    pass both (the per-source bucket is checked first so one noisy source does not drain the
    global budget of the others)."""

    def __init__(
        self,
        per_source: tuple[float, float],
        global_: tuple[float, float],
        max_sources: int = TRACKED_SOURCES,
    ) -> None:
        self.per_source = per_source
        self.max_sources = max_sources
        self.global_bucket = TokenBucket(*global_)
        self._sources: OrderedDict[str, TokenBucket] = OrderedDict()

    def allow(self, source: str, now: float | None = None) -> bool:
        b = self._sources.get(source)
        if b is None:
            b = TokenBucket(*self.per_source, now=now)
            self._sources[source] = b
            if len(self._sources) > self.max_sources:
                self._sources.popitem(last=False)
        else:
            self._sources.move_to_end(source)
        if not b.take(now):
            return False
        return self.global_bucket.take(now)
