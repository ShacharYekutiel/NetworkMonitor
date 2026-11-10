"""[3] Flow collector limits (architecture section 13.3.2) and the record budget bucket.

Units of the drop counters: `rate_limited`, `queue_full` (raw queue), `parse_error`,
`no_template`, `unsupported` count datagrams or flow sets; `unknown_exporter`,
`ignored_exporter`, `exporter_limit` count datagrams (dropped before decoding);
`over_budget` and `aggregation_overflow` count records; `db_error` and `queue_full` on the
write queue count flushes (one minute of one exporter)."""

from __future__ import annotations

import time

DEFAULT_RECORDS_PER_S = 10000
MIN_RECORDS_PER_S = 1000
MAX_RECORDS_PER_S = 50000
BUDGET_BURST_FACTOR = 2.0

EXPORTER_DATAGRAMS_PER_S = 2000.0
EXPORTER_DATAGRAMS_BURST = 4000.0
TRACKED_SOURCES = 4096

RAW_QUEUE = 20000
MAX_DATAGRAM = 65535
WORKER_BATCH = 500
MAX_EXPORTERS = 256

TEMPLATES_PER_DOMAIN = 64
TEMPLATES_TOTAL = 4096
TEMPLATE_IDLE_S = 1800.0

MAX_AGG_KEYS = 300000
WRITE_QUEUE = 50
MINUTE_GRACE_S = 5.0
DB_RETRY_S = 1.0
STOP_FLUSH_S = 3.0

RCVBUF = 8 * 1024 * 1024
RETRY_RECEIVERS_S = 60.0

UNSAMPLED_WARN_RECORDS_PER_S = 5000.0

DROP_REASONS = (
    "rate_limited",
    "queue_full",
    "parse_error",
    "no_template",
    "unknown_exporter",
    "ignored_exporter",
    "over_budget",
    "unsupported",
    "exporter_limit",
    "aggregation_overflow",
    "db_error",
)


class RecordBudget:
    """Global records/s token bucket (burst 2x). `take(n)` returns how many of `n` records
    fit; the rest are `over_budget`. Used by the single worker thread only."""

    __slots__ = ("rate", "burst", "tokens", "stamp")

    def __init__(self, rate: float, now: float | None = None) -> None:
        self.rate = float(rate)
        self.burst = float(rate) * BUDGET_BURST_FACTOR
        self.tokens = self.burst
        self.stamp = time.monotonic() if now is None else now

    def set_rate(self, rate: float) -> None:
        self.rate = float(rate)
        self.burst = float(rate) * BUDGET_BURST_FACTOR
        self.tokens = min(self.tokens, self.burst)

    def take(self, n: int, now: float | None = None) -> int:
        t = time.monotonic() if now is None else now
        elapsed = t - self.stamp
        if elapsed > 0:
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.stamp = t
        ok = min(n, int(self.tokens))
        if ok > 0:
            self.tokens -= ok
        return max(ok, 0)


def clamp_budget(value: object) -> int:
    try:
        v = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_RECORDS_PER_S
    return max(MIN_RECORDS_PER_S, min(MAX_RECORDS_PER_S, v))
