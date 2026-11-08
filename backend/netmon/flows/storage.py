"""[3] Flow storage: bootstrap of the hourly objects (continuous aggregates on TimescaleDB,
same-named plain tables otherwise), the batch writer SQL, the fallback hourly rollup and
retention (architecture 13.3.6, data-model "1-minute tables")."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from netmon.flows.aggregate import Flush

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.flows.storage")

MINUTE_TABLES = ("flow_if_1m", "flow_app_1m", "flow_host_1m", "flow_conv_1m")
HOURLY = {
    "flow_if_1h": ("flow_if_1m", ("if_index", "direction")),
    "flow_app_1h": ("flow_app_1m", ("protocol", "port")),
    "flow_host_1h": ("flow_host_1m", ("ip", "direction", "other")),
}
HOURLY_DDL = {
    "flow_if_1h": "if_index integer NOT NULL, direction text NOT NULL",
    "flow_app_1h": "protocol smallint NOT NULL, port integer NOT NULL",
    "flow_host_1h": "ip inet NOT NULL, direction text NOT NULL, other boolean NOT NULL",
}
CHUNK = "6 hours"
CAGG_START_OFFSET_H = 6
ROLLUP_HOURS = 3
BATCH = 50000


def _short(exc: BaseException) -> str:
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__


def retention_values(cfg: dict[str, Any]) -> tuple[int, int, int]:
    """(raw hours, minute hours, rollup days) from the settings, within the contract limits
    (minute retention >= 24 h, above the continuous-aggregate start offset)."""

    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(cfg.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    return (
        num("flow_raw_retention_hours", 24, 1, 168),
        num("flow_minute_retention_hours", 48, 24, 336),
        num("flow_rollup_retention_days", 30, 7, 400),
    )


# ================================================================ bootstrap
async def _relkind(conn: AsyncConnection, name: str) -> str | None:
    row = (
        await conn.execute(
            text(
                "SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = :n AND n.nspname = current_schema()"
            ),
            {"n": name},
        )
    ).first()
    return str(row[0]) if row is not None else None


async def _is_cagg(conn: AsyncConnection, name: str) -> bool:
    try:
        async with conn.begin_nested():
            row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM timescaledb_information.continuous_aggregates "
                        "WHERE view_name = :n"
                    ),
                    {"n": name},
                )
            ).first()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


async def bootstrap(
    engine: AsyncEngine, timescale: bool, cfg: dict[str, Any]
) -> tuple[str | None, bool]:
    """Returns (mode, policies_ok): mode "cagg" | "table" | None (tables missing: migration
    0007 not applied); policies_ok = Timescale retention policies are in place for every
    flow object (else the hourly retention loop deletes old rows)."""
    async with engine.connect() as conn:
        if await _relkind(conn, "flow_if_1m") is None:
            log.warning("Flow tables are missing (migration 0007 not applied)")
            return None, False
        mode = "table"
        policies = False
        if timescale:
            try:
                mode = await _try_cagg(conn, cfg)
            except Exception as exc:  # noqa: BLE001
                log.warning("Flow continuous aggregates unavailable (%s)", _short(exc))
                mode = "table"
        if mode == "table":
            await _drop_partial_caggs(conn)
            await _create_tables(conn)
        await conn.commit()
    if mode == "cagg":
        policies = await apply_policies(engine, cfg)
    log.info("Flow hourly rollups: %s (retention policies: %s)", mode, policies)
    return mode, policies


async def _try_cagg(conn: AsyncConnection, cfg: dict[str, Any]) -> str:
    for name in HOURLY:
        kind = await _relkind(conn, name)
        if kind == "r":  # plain tables of an earlier fallback run: stay in table mode
            return "table"
    for table in MINUTE_TABLES:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    f"SELECT create_hypertable('{table}', 'time', chunk_time_interval => "
                    f"INTERVAL '{CHUNK}', if_not_exists => TRUE, migrate_data => TRUE, "
                    "create_default_indexes => FALSE)"
                )
            )
    for name, (source, keys) in HOURLY.items():
        cols = ", ".join(keys)
        group = ", ".join(str(i) for i in range(1, len(keys) + 3))
        async with conn.begin_nested():
            await conn.execute(
                text(
                    f"CREATE MATERIALIZED VIEW IF NOT EXISTS {name} WITH (timescaledb.continuous, "
                    "timescaledb.materialized_only = false) AS "
                    f"SELECT time_bucket(INTERVAL '1 hour', time) AS time, exporter_id, {cols}, "
                    "sum(bytes)::bigint AS bytes, sum(packets)::bigint AS packets, "
                    "sum(flows)::bigint AS flows "
                    f"FROM {source} GROUP BY {group} WITH NO DATA"
                )
            )
        if not await _is_cagg(conn, name):
            raise RuntimeError(f"{name} is not a continuous aggregate")
        async with conn.begin_nested():
            await conn.execute(
                text(
                    f"SELECT add_continuous_aggregate_policy('{name}', start_offset => "
                    f"INTERVAL '{CAGG_START_OFFSET_H} hours', end_offset => INTERVAL '1 hour', "
                    "schedule_interval => INTERVAL '15 minutes', if_not_exists => TRUE)"
                )
            )
    return "cagg"


async def _drop_partial_caggs(conn: AsyncConnection) -> None:
    for name in HOURLY:
        if await _relkind(conn, name) == "v":
            try:
                async with conn.begin_nested():
                    await conn.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {name} CASCADE"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot drop %s: %s", name, _short(exc))


async def _create_tables(conn: AsyncConnection) -> None:
    for name, (_, keys) in HOURLY.items():
        pk = ", ".join(("exporter_id", *keys, "time"))
        await conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {name} (time timestamptz NOT NULL, "
                f"exporter_id integer NOT NULL, {HOURLY_DDL[name]}, bytes bigint NOT NULL, "
                f"packets bigint NOT NULL, flows bigint NOT NULL, PRIMARY KEY ({pk}))"
            )
        )
        await conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{name}_time ON {name} (time)"))


async def apply_policies(engine: AsyncEngine, cfg: dict[str, Any]) -> bool:
    """(Re-)apply the Timescale retention policies (minute hypertables, conversations, hourly
    aggregates). False when any policy cannot be set."""
    raw_h, minute_h, rollup_d = retention_values(cfg)
    plan = [
        ("flow_if_1m", f"{minute_h} hours"),
        ("flow_app_1m", f"{minute_h} hours"),
        ("flow_host_1m", f"{minute_h} hours"),
        ("flow_conv_1m", f"{raw_h} hours"),
        ("flow_if_1h", f"{rollup_d} days"),
        ("flow_app_1h", f"{rollup_d} days"),
        ("flow_host_1h", f"{rollup_d} days"),
    ]
    ok = True
    async with engine.connect() as conn:
        for name, interval in plan:
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        text(f"SELECT remove_retention_policy('{name}', if_exists => TRUE)")
                    )
                    await conn.execute(
                        text(f"SELECT add_retention_policy('{name}', INTERVAL '{interval}')")
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot set the retention policy of %s: %s", name, _short(exc))
                ok = False
        await conn.commit()
    return ok


# ================================================================ writer
INSERT_IF = """
INSERT INTO flow_if_1m (time, exporter_id, if_index, direction, bytes, packets, flows)
SELECT * FROM unnest(CAST(:t AS timestamptz[]), CAST(:e AS integer[]), CAST(:k1 AS integer[]),
                     CAST(:k2 AS text[]), CAST(:b AS bigint[]), CAST(:p AS bigint[]),
                     CAST(:f AS integer[]))
ON CONFLICT (exporter_id, if_index, direction, time) DO UPDATE SET
  bytes = flow_if_1m.bytes + EXCLUDED.bytes, packets = flow_if_1m.packets + EXCLUDED.packets,
  flows = flow_if_1m.flows + EXCLUDED.flows
"""
INSERT_APP = """
INSERT INTO flow_app_1m (time, exporter_id, protocol, port, bytes, packets, flows)
SELECT * FROM unnest(CAST(:t AS timestamptz[]), CAST(:e AS integer[]), CAST(:k1 AS smallint[]),
                     CAST(:k2 AS integer[]), CAST(:b AS bigint[]), CAST(:p AS bigint[]),
                     CAST(:f AS integer[]))
ON CONFLICT (exporter_id, protocol, port, time) DO UPDATE SET
  bytes = flow_app_1m.bytes + EXCLUDED.bytes, packets = flow_app_1m.packets + EXCLUDED.packets,
  flows = flow_app_1m.flows + EXCLUDED.flows
"""
INSERT_HOST = """
INSERT INTO flow_host_1m (time, exporter_id, ip, direction, other, bytes, packets, flows)
SELECT * FROM unnest(CAST(:t AS timestamptz[]), CAST(:e AS integer[]),
                     CAST(CAST(:k1 AS text[]) AS inet[]), CAST(:k2 AS text[]),
                     CAST(:k3 AS boolean[]), CAST(:b AS bigint[]), CAST(:p AS bigint[]),
                     CAST(:f AS integer[]))
ON CONFLICT (exporter_id, ip, direction, other, time) DO UPDATE SET
  bytes = flow_host_1m.bytes + EXCLUDED.bytes, packets = flow_host_1m.packets + EXCLUDED.packets,
  flows = flow_host_1m.flows + EXCLUDED.flows
"""
INSERT_CONV = """
INSERT INTO flow_conv_1m (time, exporter_id, in_if, out_if, src_ip, dst_ip, protocol, src_port,
                          dst_port, other, bytes, packets, flows)
SELECT * FROM unnest(CAST(:t AS timestamptz[]), CAST(:e AS integer[]), CAST(:k1 AS integer[]),
                     CAST(:k2 AS integer[]), CAST(CAST(:k3 AS text[]) AS inet[]),
                     CAST(CAST(:k4 AS text[]) AS inet[]), CAST(:k5 AS smallint[]),
                     CAST(:k6 AS integer[]), CAST(:k7 AS integer[]), CAST(:k8 AS boolean[]),
                     CAST(:b AS bigint[]), CAST(:p AS bigint[]), CAST(:f AS integer[]))
"""


def _merge(rows: list[tuple[Any, ...]], nkeys: int) -> list[tuple[Any, ...]]:
    """Rows with the same key (same exporter/minute flushed twice in one batch) summed, so one
    INSERT ... ON CONFLICT never touches a row twice."""
    acc: dict[tuple[Any, ...], list[int]] = {}
    for r in rows:
        key = r[:nkeys]
        v = acc.get(key)
        if v is None:
            acc[key] = [r[nkeys], r[nkeys + 1], r[nkeys + 2]]
        else:
            v[0] += r[nkeys]
            v[1] += r[nkeys + 1]
            v[2] += r[nkeys + 2]
    return [(*k, *v) for k, v in acc.items()]


def _columns(rows: list[tuple[Any, ...]], nkeys: int) -> dict[str, list[Any]]:
    cols: dict[str, list[Any]] = {"t": [], "e": []}
    for i in range(1, nkeys - 1):
        cols[f"k{i}"] = []
    cols.update({"b": [], "p": [], "f": []})
    names = list(cols)
    for r in rows:
        for name, v in zip(names, r, strict=True):
            cols[name].append(v)
    return cols


def flush_rows(
    batch: list[tuple[int, Flush]],
) -> dict[str, list[tuple[Any, ...]]]:
    """[(exporter_id, Flush)] -> rows per table: (time, exporter_id, *keys, bytes, packets,
    flows)."""
    out: dict[str, list[tuple[Any, ...]]] = {k: [] for k in MINUTE_TABLES}
    for eid, f in batch:
        t = datetime.fromtimestamp(f.minute, UTC)
        out["flow_if_1m"].extend((t, eid, *r) for r in f.if_rows)
        out["flow_app_1m"].extend((t, eid, *r) for r in f.app_rows)
        out["flow_host_1m"].extend((t, eid, *r) for r in f.host_rows)
        out["flow_conv_1m"].extend((t, eid, *r) for r in f.conv_rows)
    out["flow_if_1m"] = _merge(out["flow_if_1m"], 4)
    out["flow_app_1m"] = _merge(out["flow_app_1m"], 4)
    out["flow_host_1m"] = _merge(out["flow_host_1m"], 5)
    return out


async def write_batch(rt: Runtime, batch: list[tuple[int, Flush]]) -> int:
    """One transaction for a batch of flushes. Returns the rows written."""
    rows = flush_rows(batch)
    n = 0
    async with rt.sessionmaker() as session:
        for table, sql, nkeys in (
            ("flow_if_1m", INSERT_IF, 4),
            ("flow_app_1m", INSERT_APP, 4),
            ("flow_host_1m", INSERT_HOST, 5),
            ("flow_conv_1m", INSERT_CONV, 10),
        ):
            if rows[table]:
                await session.execute(text(sql), _columns(rows[table], nkeys))
                n += len(rows[table])
        await session.commit()
    return n


# ================================================================ rollup / retention
async def rollup(rt: Runtime, now: datetime | None = None, hours: int = ROLLUP_HOURS) -> int:
    """Fallback mode: (re)compute the last `hours` complete hours from the 1-minute tables
    (idempotent)."""
    now = now or rt.clock.now()
    to = now.replace(minute=0, second=0, microsecond=0)
    frm = to - timedelta(hours=hours)
    n = 0
    async with rt.sessionmaker() as session:
        for name, (source, keys) in HOURLY.items():
            cols = ", ".join(keys)
            group = ", ".join(str(i) for i in range(1, len(keys) + 3))
            pk = ", ".join(("exporter_id", *keys, "time"))
            res = await session.execute(
                text(
                    f"INSERT INTO {name} (time, exporter_id, {cols}, bytes, packets, flows) "
                    "SELECT date_bin(INTERVAL '1 hour', time, TIMESTAMPTZ '2000-01-01'), "
                    f"exporter_id, {cols}, sum(bytes), sum(packets), sum(flows) "
                    f"FROM {source} WHERE time >= :frm AND time < :to GROUP BY {group} "
                    f"ON CONFLICT ({pk}) DO UPDATE SET bytes = EXCLUDED.bytes, "
                    "packets = EXCLUDED.packets, flows = EXCLUDED.flows"
                ),
                {"frm": frm, "to": to},
            )
            n += res.rowcount or 0
        await session.commit()
    return n


async def delete_older(rt: Runtime, table: str, cutoff: datetime, batch: int = BATCH) -> int:
    total = 0
    while True:
        async with rt.sessionmaker() as session:
            res = await session.execute(
                text(
                    f"DELETE FROM {table} WHERE time < :c AND (tableoid, ctid) IN "  # noqa: S608
                    f"(SELECT tableoid, ctid FROM {table} WHERE time < :c LIMIT :n)"
                ),
                {"c": cutoff, "n": batch},
            )
            await session.commit()
        n = res.rowcount or 0
        total += n
        if n < batch:
            return total
        await asyncio.sleep(0)


async def delete_exporter_rows(rt: Runtime, exporter_id: int, batch: int = BATCH) -> int:
    """Background delete of a removed exporter's rows (1-minute tables; hourly tables in table
    mode; continuous aggregates keep their hours until retention, queries skip them)."""
    tables = list(MINUTE_TABLES)
    if getattr(rt, "flows_rollup", None) == "table":
        tables += list(HOURLY)
    total = 0
    for table in tables:
        while True:
            try:
                async with rt.sessionmaker() as session:
                    res = await session.execute(
                        text(
                            f"DELETE FROM {table} WHERE exporter_id = :e AND (tableoid, ctid) IN "  # noqa: S608
                            f"(SELECT tableoid, ctid FROM {table} WHERE exporter_id = :e LIMIT :n)"
                        ),
                        {"e": exporter_id, "n": batch},
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001
                log.exception("Deleting the %s rows of exporter %s failed", table, exporter_id)
                break
            n = res.rowcount or 0
            total += n
            if n < batch:
                break
            await asyncio.sleep(0)
    return total


async def run_retention(rt: Runtime, cfg: dict[str, Any]) -> dict[str, int]:
    """Hourly (from the retention loop, whether or not `flows` is ON): fallback rollup of the
    last 3 complete hours, then expired rows in batches (Timescale policies do it otherwise)."""
    out = {"flow_rollup": 0, "flow_rows": 0}
    mode = getattr(rt, "flows_rollup", None)
    if mode is None:
        return out
    try:
        if mode == "table":
            out["flow_rollup"] = await rollup(rt)
        if mode == "cagg" and getattr(rt, "flows_policies_ok", False):
            return out
        raw_h, minute_h, rollup_d = retention_values(cfg)
        now = rt.clock.now()
        plan = [
            ("flow_conv_1m", now - timedelta(hours=raw_h)),
            ("flow_if_1m", now - timedelta(hours=minute_h)),
            ("flow_app_1m", now - timedelta(hours=minute_h)),
            ("flow_host_1m", now - timedelta(hours=minute_h)),
        ]
        if mode == "table":
            plan += [(name, now - timedelta(days=rollup_d)) for name in HOURLY]
        for table, cutoff in plan:
            out["flow_rows"] += await delete_older(rt, table, cutoff)
    except Exception as exc:  # noqa: BLE001
        log.warning("Flow rollup/retention failed: %s", _short(exc))
    if out["flow_rows"]:
        log.info("Retention removed %d flow rows", out["flow_rows"])
    return out


async def storage_bytes(rt: Runtime) -> int | None:
    """Total size of the flow objects (hypertables: hypertable_size)."""
    names = [*MINUTE_TABLES, *HOURLY]
    total = 0
    try:
        async with rt.sessionmaker() as session:
            for name in names:
                size = None
                if rt.timescale_enabled:
                    try:
                        async with session.begin_nested():
                            size = (
                                await session.execute(
                                    text("SELECT hypertable_size(CAST(:n AS regclass))"),
                                    {"n": name},
                                )
                            ).scalar()
                    except Exception:  # noqa: BLE001
                        size = None
                if size is None:
                    size = (
                        await session.execute(
                            text(
                                "SELECT CASE WHEN to_regclass(:n) IS NULL THEN 0 "
                                "ELSE pg_total_relation_size(to_regclass(:n)) END"
                            ),
                            {"n": name},
                        )
                    ).scalar()
                total += int(size or 0)
    except Exception:  # noqa: BLE001
        return None
    return total
