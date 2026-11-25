"""[syslog-protection] Cleaning what is already stored: the rows a syslog filter rule matches
(`drop`), or the rows beyond the newest `keep_n` per source (`keep`). Offered after a rule is
created or changed, never automatic: the preview counts the rows up to a point in time and
the cleanup deletes exactly those (rows stored later are never touched).

Built for millions of rows in both database modes: the delete walks the time axis in slices
(`ix_syslog_time` / the hypertable chunks) and removes at most `BATCH` rows per statement and
transaction, so there is no long table lock and the ingest writer keeps going in between."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from netmon.ingest.syslog_protect import rule_sql
from netmon.services.serialize import iso

log = logging.getLogger("netmon.ingest.syslog_cleanup")

BATCH = 5000
SLICE = timedelta(hours=1)
PAUSE_S = 0.01  # between statements: leave room for the ingest writer

_CUT_JOIN = (
    " JOIN unnest(CAST(:c_s AS text[]), CAST(:c_t AS timestamptz[]), CAST(:c_i AS bigint[]))"
    " AS c(s, t, i) ON x.source_ip = CAST(c.s AS inet) AND (x.time, x.id) < (c.t, c.i)"
)


async def preview(session: Any, rule: dict[str, Any], until: datetime) -> int:
    where, p = rule_sql(rule)
    p["until"] = until
    if rule["action"] == "keep":
        p["n"] = int(rule["keep_n"])
        sql = (
            "SELECT coalesce(sum(greatest(z.c - :n, 0)), 0) FROM (SELECT count(*) AS c "  # noqa: S608
            f"FROM syslog_messages x WHERE {where} AND x.time <= :until GROUP BY x.source_ip) z"
        )
    else:
        sql = f"SELECT count(*) FROM syslog_messages x WHERE {where} AND x.time <= :until"  # noqa: S608
    return int((await session.execute(text(sql), p)).scalar_one())


async def _cutoffs(rt: Any, where: str, p: dict[str, Any], n: int) -> dict[str, list[Any]]:
    """The n-th newest matching row per source (kept; older ones go)."""
    async with rt.sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT host(z.s) AS cs, z.t AS ct, z.i AS ci FROM (SELECT x.source_ip AS s, "  # noqa: S608
                    "x.time AS t, x.id AS i, row_number() OVER (PARTITION BY x.source_ip "
                    "ORDER BY x.time DESC, x.id DESC) AS rn FROM syslog_messages x "
                    f"WHERE {where} AND x.time <= :until) z WHERE z.rn = :n"
                ),
                {**p, "n": n},
            )
        ).all()
    # (Row.t is SQLAlchemy's tuple accessor: the columns are named cs / ct / ci)
    return {
        "c_s": [r.cs for r in rows],
        "c_t": [r.ct for r in rows],
        "c_i": [int(r.ci) for r in rows],
    }


async def run(rt: Any, rule: dict[str, Any], until: datetime, job: dict[str, Any]) -> int:
    """Delete in time slices and batches; `job["deleted"]` follows the progress."""
    where, p = rule_sql(rule)
    p["until"] = until
    keep = rule["action"] == "keep"
    join = ""
    try:
        if keep:
            cut = await _cutoffs(rt, where, p, int(rule["keep_n"]))
            if not cut["c_s"]:
                return await _finish(rt, rule, where, p, job)
            p.update(cut)
            join = _CUT_JOIN
        async with rt.sessionmaker() as session:
            start = (
                await session.execute(
                    text(
                        f"SELECT min(x.time) FROM syslog_messages x{join} "  # noqa: S608
                        f"WHERE {where} AND x.time <= :until"
                    ),
                    p,
                )
            ).scalar_one()
        a = start
        while a is not None and a <= until:
            b = min(a + SLICE, until + timedelta(microseconds=1))
            while True:
                async with rt.sessionmaker() as session:
                    res = await session.execute(
                        text(
                            "DELETE FROM syslog_messages t WHERE (t.id, t.time) IN ("  # noqa: S608
                            f"SELECT x.id, x.time FROM syslog_messages x{join} WHERE {where} "
                            "AND x.time >= :a AND x.time < :b LIMIT :lim)"
                        ),
                        {**p, "a": a, "b": b, "lim": BATCH},
                    )
                    await session.commit()
                n = int(res.rowcount or 0)
                job["deleted"] += n
                await asyncio.sleep(PAUSE_S)
                if n < BATCH:
                    break
            a = b
        return await _finish(rt, rule, where, p, job)
    except asyncio.CancelledError:
        job.update(state="failed", error="stopped", finished_at=iso(rt.clock.now()))
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Syslog cleanup of filter rule %s failed: %s", rule["id"], exc)
        job.update(state="failed", error=str(exc)[:300], finished_at=iso(rt.clock.now()))
        return int(job["deleted"])


async def _finish(
    rt: Any, rule: dict[str, Any], where: str, p: dict[str, Any], job: dict[str, Any]
) -> int:
    if rule["action"] == "keep":
        # the newest N that stay now count towards the rule's window: tag them so the
        # per-write prune keeps the total at N per source
        q = {k: v for k, v in p.items() if not k.startswith("c_")}
        async with rt.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE syslog_messages x SET filter_rule_id = :rid "  # noqa: S608
                    f"WHERE {where} AND x.time <= :until AND x.filter_rule_id IS DISTINCT FROM :rid"
                ),
                {**q, "rid": int(rule["id"])},
            )
            await session.commit()
    job.update(state="done", finished_at=iso(rt.clock.now()))
    if job["deleted"]:
        log.info("Syslog cleanup of filter rule %s removed %d row(s)", rule["id"], job["deleted"])
    return int(job["deleted"])
