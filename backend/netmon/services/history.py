"""History queries with time bucketing (time_bucket in Timescale mode, date_bin in fallback)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.services.serialize import iso

RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
ORIGIN = "TIMESTAMPTZ '2000-01-01 00:00:00+00'"


def bucket_for_span(span_s: float) -> int:
    if span_s <= 3600:
        return 60
    if span_s <= 86400:
        return 300
    if span_s <= 7 * 86400:
        return 1800
    if span_s <= 30 * 86400:
        return 7200
    return int(math.ceil(span_s / 400 / 60.0)) * 60


def bucket_expr(timescale: bool) -> str:
    width = "(CAST(:bucket AS integer) * INTERVAL '1 second')"
    if timescale:
        return f"time_bucket({width}, time, origin => {ORIGIN})"
    return f"date_bin({width}, time, {ORIGIN})"


async def query_history(
    session: AsyncSession,
    *,
    timescale: bool,
    sensor_id: int,
    channels: list[tuple[str, str]],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    span = (end - start).total_seconds()
    bucket = bucket_for_span(span)
    keys = [k for k, _ in channels]
    points: dict[str, list[dict[str, Any]]] = {k: [] for k in keys}
    if keys:
        sql = text(
            f"""
            SELECT {bucket_expr(timescale)} AS t, channel,
                   avg(value) AS avg, min(value) AS min, max(value) AS max
            FROM measurements
            WHERE sensor_id = :sid AND channel = ANY(:chans) AND time >= :start AND time <= :end
            GROUP BY 1, 2 ORDER BY 2, 1
            """
        )
        rows = await session.execute(
            sql, {"bucket": bucket, "sid": sensor_id, "chans": keys, "start": start, "end": end}
        )
        for r in rows:
            points[r.channel].append(
                {"t": iso(r.t), "avg": float(r.avg), "min": float(r.min), "max": float(r.max)}
            )
    return {
        "sensor_id": sensor_id,
        "from": iso(start),
        "to": iso(end),
        "bucket_s": bucket,
        "channels": [{"key": k, "unit": u, "points": points[k]} for k, u in channels],
    }


def range_bounds(range_: str, now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(seconds=RANGES[range_]), now
