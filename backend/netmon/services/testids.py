"""[4] Shared configure-first `test_id` registry (owner rule "configure-first", architecture
§ 15.0). Moved from `netmon.assistant.testconn.TestRegistry` (PR 4-0a; re-exported there).

A `test_id` (128-bit, 30 min, in memory) is bound to `(kind, user id, SHA-256 of the canonical
connection fields)`; kinds: `assistant`, `kb`, `aws_connection` (later `aws_inventory` …).
The per-admin rate limit (10 tests per 10 min) is shared by every kind."""

from __future__ import annotations

import secrets
import time
from collections import deque
from datetime import UTC, datetime, timedelta

TEST_TTL = timedelta(minutes=30)
RATE_LIMIT = 10
RATE_WINDOW_S = 600.0


class TestRegistry:
    """test_ids (in memory) and the per-admin rate limit (10 tests per 10 min)."""

    def __init__(self) -> None:
        self._ids: dict[str, tuple[str, int, str, datetime]] = {}
        self._hits: dict[int, deque[float]] = {}

    def issue(
        self, kind: str, user_id: int, conn_hash: str, now: datetime | None = None
    ) -> tuple[str, datetime]:
        now = now or datetime.now(UTC)
        self._purge(now)
        tid = secrets.token_hex(16)
        exp = now + TEST_TTL
        self._ids[tid] = (kind, user_id, conn_hash, exp)
        return tid, exp

    def valid(
        self,
        kind: str,
        test_id: str | None,
        user_id: int,
        conn_hash: str,
        now: datetime | None = None,
    ) -> bool:
        if not test_id:
            return False
        now = now or datetime.now(UTC)
        entry = self._ids.get(test_id)
        if entry is None:
            return False
        k, uid, h, exp = entry
        return k == kind and uid == user_id and secrets.compare_digest(h, conn_hash) and now < exp

    def consume(self, test_id: str) -> None:
        self._ids.pop(test_id, None)

    def _purge(self, now: datetime) -> None:
        for k in [k for k, v in self._ids.items() if v[3] <= now]:
            del self._ids[k]

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        q = self._hits.setdefault(user_id, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True

    def clear(self) -> None:
        self._ids.clear()
        self._hits.clear()
