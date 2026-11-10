"""Apply a device template: create the missing sensors (idempotent).

Shared by `POST /api/devices/{id}/apply-template` and the discovery importer."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from netmon.api.errors import ApiError
from netmon.models import Device, Sensor
from netmon.poller.processor import execute_check
from netmon.poller.sensor_types import (
    get_type,
    validate_channel_overrides,
    validate_config,
)
from netmon.poller.snmp_client import (
    SnmpError,
    as_text,
    snmp_get_typed,
    snmp_walk,
    walk_to_index,
)
from netmon.poller.types import CheckContext
from netmon.services import settings as settings_svc
from netmon.services.sensors import build_channels, effective_credentials
from netmon.services.snmp_enum import enumerate_interfaces

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.templates")

BUDGET_S = 120.0
PROBES_PER_DEVICE = 4
MAX_TABLE_ROWS = 1000


def identity(type_: str, config: dict[str, Any]) -> tuple[Any, ...] | None:
    """The idempotency key of a sensor (docs/data-model.md "Idempotency")."""
    if type_ in ("ping", "snmp_system", "snmp_cpu", "snmp_memory"):
        return (type_,)
    if type_ == "snmp_interface":
        try:
            return (type_, int(config.get("if_index")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    if type_ == "snmp_disk":
        return (type_, config.get("storage_descr"))
    if type_ == "snmp_custom":
        # [2b-1] percent_of_oid is part of the identity: the HPE templates read the same
        # column as a percentage (PoE usage) and as a plain value (PoE power)
        pct = str(config.get("percent_of_oid") or "").lstrip(".") or None
        return (type_, str(config.get("oid") or "").lstrip("."), pct)
    if type_ == "snmp_walk_count":  # [2b-2]
        cv = config.get("count_values")
        values = tuple(sorted(cv)) if isinstance(cv, list) else None
        return (type_, str(config.get("oid") or "").lstrip("."), values)
    if type_ == "http":
        return (type_, config.get("url"))
    if type_ == "tcp_port":
        return (type_, config.get("port"))
    if type_ == "dns":
        return (type_, config.get("query_name"), config.get("record_type"))
    return None


@dataclass
class _Candidate:
    type: str
    name: str
    config: dict[str, Any]
    overrides: dict[str, dict[str, float | None]]
    interval_s: int | None
    probe: bool
    skip: dict[str, Any] | None = None  # set when the candidate turned into a skip
    probe_result: Any = None
    when: list[dict[str, Any]] = field(default_factory=list)  # [2b-2]


@dataclass
class _Slot:
    """One output position (entries keep their order in created/skipped)."""

    candidate: _Candidate | None = None
    skip: dict[str, Any] | None = None


@dataclass
class _Ctx:
    host: str
    creds: dict[str, Any]
    lang: str
    existing: set[tuple[Any, ...]]
    deadline: float
    probe_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    # identity -> (sensor id, name) of the device's sensors, to repair ambiguous names
    existing_names: dict[tuple[Any, ...], tuple[int, str]] = field(default_factory=dict)
    renames: list[tuple[int, str, str]] = field(default_factory=list)
    entity: dict[str, str] | None = None  # entPhysicalIndex -> stack unit label (cached)


def _l10n(name: Any, lang: str) -> str:
    if isinstance(name, dict):
        return str(name.get(lang) or name.get("en") or "")
    return str(name or "")


def _skip(name: str, type_: str, reason: str, message: str = "") -> dict[str, Any]:
    return {"name": name[:128], "type": type_, "reason": reason, "message": message}


def _remaining(ctx: _Ctx) -> float:
    return ctx.deadline - asyncio.get_running_loop().time()


ENT_SENSOR_SCALE = "1.3.6.1.2.1.99.1.1.1.2"  # entPhySensorScale (EntitySensorDataScale)
ENT_SENSOR_PRECISION = "1.3.6.1.2.1.99.1.1.1.3"  # entPhySensorPrecision (-8..9)


def entity_sensor_multiplier(scale: Any, precision: Any) -> float:
    """Factor turning an entPhySensorValue into base units: scale units(9) = 10^0, each step
    10^3 (milli(8) = 10^-3, kilo(10) = 10^3); precision p = p decimal places (10^-p).
    Unreadable or out-of-range values count as units / 0."""
    try:
        sc = int(scale)
    except (TypeError, ValueError):
        sc = 9
    try:
        pr = int(precision)
    except (TypeError, ValueError):
        pr = 0
    if not 1 <= sc <= 17:
        sc = 9
    if not -8 <= pr <= 9:
        pr = 0
    return 10.0 ** (3 * (sc - 9) - pr)


async def _walk_column(rt: Runtime, ctx: _Ctx, oid: str) -> dict[str, Any]:
    async with rt.semaphore:
        rows = await asyncio.wait_for(
            snmp_walk(ctx.host, ctx.creds, oid, max_rows=MAX_TABLE_ROWS),
            max(1.0, _remaining(ctx)),
        )
    return walk_to_index(rows, oid)


def _suffix_key(s: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in s.split("."))
    except ValueError:
        return (1 << 30,)


def _sensor_candidate(entry: dict[str, Any], ctx: _Ctx) -> _Slot:
    type_ = entry["type"]
    name = _l10n(entry["name"], ctx.lang)[:128]
    cfg = validate_config(type_, dict(entry.get("config") or {}))
    if identity(type_, cfg) in ctx.existing:
        return _Slot(skip=_skip(name, type_, "exists"))
    overrides = validate_channel_overrides(type_, entry.get("channels") or [])
    return _Slot(
        candidate=_Candidate(
            type_,
            name,
            cfg,
            overrides,
            entry.get("interval_s"),
            bool(entry.get("probe")),
            when=list(entry.get("when") or []),
        )
    )


async def _interface_slots(rt: Runtime, entry: dict[str, Any], ctx: _Ctx) -> list[_Slot]:
    label = "Interfaces" if ctx.lang != "he" else "ממשקים"
    try:
        async with rt.semaphore:
            items = await asyncio.wait_for(
                enumerate_interfaces(ctx.host, ctx.creds), max(1.0, _remaining(ctx))
            )
    except TimeoutError:
        return [_Slot(skip=_skip(label, "snmp_interface", "probe_failed", "Timed out"))]
    except SnmpError as exc:
        return [_Slot(skip=_skip(label, "snmp_interface", "snmp_error", str(exc)))]
    if not items:
        return [_Slot(skip=_skip(label, "snmp_interface", "no_rows", "No interfaces found"))]
    inc = entry.get("include_name_regex")
    exc_re = entry.get("exclude_name_regex")
    inc_c = re.compile(inc, re.IGNORECASE) if inc else None
    exc_c = re.compile(exc_re, re.IGNORECASE) if exc_re else None
    excluded_types = set(entry.get("exclude_if_types") or [])
    kept = []
    for it in sorted(items, key=lambda x: x["if_index"]):
        if entry.get("oper_up_only") and it.get("oper_status") != "up":
            continue
        if it.get("type") in excluded_types:
            continue
        name = it.get("name") or it.get("descr") or ""
        if inc_c is not None and not inc_c.search(name):
            continue
        if exc_c is not None and exc_c.search(name):
            continue
        kept.append(it)
    if not kept:
        return [
            _Slot(skip=_skip(label, "snmp_interface", "no_rows", "No matching interfaces found"))
        ]
    max_n = int(entry.get("max") or len(kept))
    overrides = validate_channel_overrides("snmp_interface", entry.get("channels") or [])
    slots = []
    for it in kept[:max_n]:
        if_name = (it.get("name") or it.get("descr") or f"Interface {it['if_index']}")[:128]
        cfg = validate_config("snmp_interface", {"if_index": it["if_index"], "if_name": if_name})
        if identity("snmp_interface", cfg) in ctx.existing:
            slots.append(_Slot(skip=_skip(if_name, "snmp_interface", "exists")))
            continue
        slots.append(
            _Slot(candidate=_Candidate("snmp_interface", if_name, cfg, overrides, None, False))
        )
    rest = len(kept) - max_n
    if rest > 0:
        slots.append(
            _Slot(
                skip=_skip(
                    label,
                    "snmp_interface",
                    "max_reached",
                    f"{rest} more interfaces not added (max {max_n})",
                )
            )
        )
    return slots


ENT_PHYSICAL = "1.3.6.1.2.1.47.1.1.1.1"  # ENTITY-MIB entPhysicalEntry
ENT_CHASSIS = 3
UNIT_WORD = {"en": "Unit", "he": "יחידה"}


async def _entity_units(rt: Runtime, ctx: _Ctx) -> dict[str, str]:
    """entPhysicalIndex -> the stack unit it sits in ("Unit 2" or the chassis name), only when the
    device has more than one chassis (an IRF / VSF / stack); {} otherwise."""
    if ctx.entity is not None:
        return ctx.entity
    contained = await _walk_column(rt, ctx, f"{ENT_PHYSICAL}.4")
    classes = await _walk_column(rt, ctx, f"{ENT_PHYSICAL}.5")
    chassis = sorted((i for i, c in classes.items() if _as_int(c) == ENT_CHASSIS), key=_suffix_key)
    units: dict[str, str] = {}
    if len(chassis) > 1:
        names = await _walk_column(rt, ctx, f"{ENT_PHYSICAL}.7")
        texts = [as_text(names.get(c)).strip() for c in chassis]
        distinct = all(texts) and len(set(texts)) == len(texts)
        word = UNIT_WORD.get(ctx.lang, "Unit")
        label = {c: (texts[n] if distinct else f"{word} {n + 1}") for n, c in enumerate(chassis)}
        for idx in classes:
            cur, hops = idx, 0
            while cur in contained and hops < 32 and cur not in label:
                cur = str(_as_int(contained.get(cur)) or 0)
                hops += 1
            if cur in label:
                units[idx] = label[cur]
    ctx.entity = units
    return units


async def _unique_labels(
    rt: Runtime, ctx: _Ctx, entry: dict[str, Any], rows: list[tuple[str, str]]
) -> dict[str, str]:
    """Row labels made unique within one table entry: rows that share a name (e.g. "PSU 1" on
    every member of an IRF stack) get their stack unit (ENTITY-MIB tables), then "#n"."""
    out = dict(rows)
    if all(n == 1 for n in Counter(out.values()).values()):
        return out
    if str(entry.get("label_oid") or "").strip(".").startswith(ENT_PHYSICAL):
        try:
            units = await _entity_units(rt, ctx)
        except (TimeoutError, SnmpError):
            units = {}
        counts = Counter(out.values())
        for s, text in rows:
            if counts[text] > 1 and units.get(s):
                out[s] = f"{text[:44]} · {units[s]}"[:64]
    counts = Counter(out.values())
    seen: Counter[str] = Counter()
    for s, _ in rows:
        text = out[s]
        if counts[text] > 1:
            seen[text] += 1
            out[s] = f"{text[:58]} #{seen[text]}"
    return out


async def _table_slots(rt: Runtime, entry: dict[str, Any], ctx: _Ctx) -> list[_Slot]:
    name_tpl = _l10n(entry["name"], ctx.lang)
    label = re.sub(r"\s+", " ", name_tpl.replace("{label}", "").replace("{index}", "")).strip()
    label = label or "SNMP"
    walk_oid = str(entry["walk_oid"]).strip(".")
    try:
        values = await _walk_column(rt, ctx, walk_oid)
        labels = await _walk_column(rt, ctx, entry["label_oid"]) if entry.get("label_oid") else {}
        labels2 = (
            await _walk_column(rt, ctx, entry["label2_oid"]) if entry.get("label2_oid") else {}
        )
        scales: dict[str, Any] = {}
        precisions: dict[str, Any] = {}
        if entry.get("entity_sensor_scale"):
            scales = await _walk_column(rt, ctx, ENT_SENSOR_SCALE)
            precisions = await _walk_column(rt, ctx, ENT_SENSOR_PRECISION)
    except TimeoutError:
        return [_Slot(skip=_skip(label, "snmp_custom", "probe_failed", "Timed out"))]
    except SnmpError as exc:
        return [_Slot(skip=_skip(label, "snmp_custom", "snmp_error", str(exc)))]
    if not values:
        return [_Slot(skip=_skip(label, "snmp_custom", "no_rows", "The table has no rows"))]
    suffixes = sorted(values, key=_suffix_key)
    if entry.get("skip_zero"):
        suffixes = [x for x in suffixes if _nonzero_number(values[x])]
    filters = list(entry.get("filters") or [])
    if filters and suffixes:
        try:
            suffixes = await _apply_filters(rt, ctx, walk_oid, values, suffixes, filters)
        except TimeoutError:
            return [_Slot(skip=_skip(label, "snmp_custom", "probe_failed", "Timed out"))]
        except SnmpError as exc:
            return [_Slot(skip=_skip(label, "snmp_custom", "snmp_error", str(exc)))]
    if not suffixes:
        return [_Slot(skip=_skip(label, "snmp_custom", "no_rows", "No matching rows"))]
    max_n = int(entry.get("max") or len(suffixes))
    overrides = validate_channel_overrides("snmp_custom", entry.get("channels") or [])
    slots = []
    base: list[tuple[str, str]] = []
    for s in suffixes[:max_n]:
        text = as_text(labels.get(s)) if s in labels else ""
        text2 = as_text(labels2.get(s)) if s in labels2 else ""
        row_label = text or s
        if text and text2:
            row_label = f"{text} ({text2})"
        base.append((s, row_label[:64]))
    unique = await _unique_labels(rt, ctx, entry, base)
    for s, plain in base:
        row_label = unique[s]
        cfg_in = {**(entry.get("config") or {}), "oid": f"{walk_oid}.{s}", "label": row_label}
        if entry.get("percent_of_column"):
            cfg_in["percent_of_oid"] = f"{str(entry['percent_of_column']).strip('.')}.{s}"
        if entry.get("entity_sensor_scale"):
            factor = entity_sensor_multiplier(scales.get(s), precisions.get(s))
            mult = float(cfg_in.get("multiplier", 1.0)) * factor
            if mult != 1.0:
                cfg_in["multiplier"] = mult
        cfg = validate_config("snmp_custom", cfg_in)
        name = name_tpl.replace("{label}", row_label).replace("{index}", s)[:128]
        key = identity("snmp_custom", cfg)
        if key in ctx.existing:
            # an earlier apply gave this sensor the ambiguous name: repair it, unless the
            # user renamed it since
            old = ctx.existing_names.get(key) if key is not None else None
            ambiguous = name_tpl.replace("{label}", plain).replace("{index}", s)[:128]
            if old is not None and old[1] == ambiguous != name:
                ctx.renames.append((old[0], name, row_label))
                slots.append(_Slot(skip=_skip(name, "snmp_custom", "exists", "Renamed")))
            else:
                slots.append(_Slot(skip=_skip(name, "snmp_custom", "exists")))
            continue
        slots.append(
            _Slot(
                candidate=_Candidate(
                    "snmp_custom", name, cfg, overrides, entry.get("interval_s"), False
                )
            )
        )
    rest = len(suffixes) - max_n
    if rest > 0:
        slots.append(
            _Slot(
                skip=_skip(
                    label, "snmp_custom", "max_reached", f"{rest} more rows not added (max {max_n})"
                )
            )
        )
    return slots


def _nonzero_number(v: Any) -> bool:
    """skip_zero: rows whose value is 0 or not numeric are ignored."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int | float):
        return v != 0
    try:
        return float(as_text(v)) != 0
    except ValueError:
        return False


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(as_text(v))
    except ValueError:
        return None


FILTER_GET_CHUNK = 16


async def _apply_filters(
    rt: Runtime,
    ctx: _Ctx,
    walk_oid: str,
    values: dict[str, Any],
    suffixes: list[str],
    filters: list[dict[str, Any]],
) -> list[str]:
    """Keep the rows whose `oid.S` value (integer) is in `in` / not in `not_in` for every
    filter. A missing or non-integer value drops the row. A filter on the walked column reuses
    the walked values; others are fetched with GETs (in chunks)."""
    kept = list(suffixes)
    for f in filters:
        oid = str(f.get("oid") or "").strip(".")
        if not oid or not kept:
            continue
        if oid == walk_oid:
            got = {x: values.get(x) for x in kept}
        else:
            got = {}
            for i in range(0, len(kept), FILTER_GET_CHUNK):
                chunk = kept[i : i + FILTER_GET_CHUNK]
                oids = [f"{oid}.{x}" for x in chunk]
                async with rt.semaphore:
                    res = await asyncio.wait_for(
                        snmp_get_typed(ctx.host, ctx.creds, oids), max(1.0, _remaining(ctx))
                    )
                for x, o in zip(chunk, oids, strict=True):
                    got[x] = res.get(o, (None, ""))[0]
        allowed, denied = f.get("in"), f.get("not_in")
        nxt = []
        for x in kept:
            v = _as_int(got.get(x))
            if v is None:
                continue
            if allowed is not None and v not in allowed:
                continue
            if denied is not None and v in denied:
                continue
            nxt.append(x)
        kept = nxt
    return kept


def when_passes(filters: list[dict[str, Any]], values: dict[str, Any]) -> str | None:
    """[2b-2] None when every condition holds, else the OID of the first failing one. A value
    must be an integer passing `in` / `not_in`; a missing value fails."""
    for f in filters:
        oid = str(f.get("oid") or "").strip().lstrip(".")
        v = _as_int(values.get(oid)) if values.get(oid) is not None else None
        allowed, denied = f.get("in"), f.get("not_in")
        if v is None or (allowed is not None and v not in allowed):
            return oid
        if denied is not None and v in denied:
            return oid
    return None


async def _check_when(rt: Runtime, ctx: _Ctx, cand: _Candidate) -> None:
    """[2b-2] One GET for all `when` OIDs of the entry; a failure -> skipped not_applicable."""
    oids = [str(f.get("oid") or "").strip().lstrip(".") for f in cand.when]
    if not oids:
        return
    values: dict[str, Any] = {}
    async with ctx.probe_sem:
        try:
            if _remaining(ctx) <= 0:
                raise TimeoutError
            async with rt.semaphore:
                res = await asyncio.wait_for(
                    snmp_get_typed(ctx.host, ctx.creds, oids), max(1.0, _remaining(ctx))
                )
            values = {o: v for o, (v, _) in res.items()}
        except (TimeoutError, SnmpError):
            values = {}
    failed = when_passes(cand.when, values)
    if failed is not None:
        cand.skip = _skip(cand.name, cand.type, "not_applicable", f"Condition not met: {failed}")


async def _run_probe(rt: Runtime, ctx: _Ctx, cand: _Candidate) -> None:
    remaining = _remaining(ctx)
    if remaining <= 0:
        cand.skip = _skip(cand.name, cand.type, "probe_failed", "Timed out")
        return
    tdef = get_type(cand.type)
    check = CheckContext(
        None,
        cand.type,
        dict(cand.config),
        ctx.host,
        ctx.creds if tdef.requires_snmp else {},
        {},
        timeout_s=min(60.0, max(1.0, remaining)),
    )
    async with ctx.probe_sem:
        if _remaining(ctx) <= 0:
            cand.skip = _skip(cand.name, cand.type, "probe_failed", "Timed out")
            return
        try:
            raw, _, _ = await asyncio.wait_for(
                execute_check(check, rt.semaphore), max(1.0, _remaining(ctx))
            )
        except TimeoutError:
            cand.skip = _skip(cand.name, cand.type, "probe_failed", "Timed out")
            return
    if not raw.ok:
        cand.skip = _skip(cand.name, cand.type, "probe_failed", raw.message or "Check failed")
        return
    if cand.type == "snmp_walk_count" and not (raw.values.get("rows") or 0) >= 1:
        cand.skip = _skip(cand.name, cand.type, "probe_failed", "No rows")  # [2b-2]
        return
    cand.probe_result = raw
    if raw.config_update:
        cand.config = {**cand.config, **raw.config_update}


_device_locks: dict[int, tuple[asyncio.Lock, int]] = {}


async def apply_template(
    rt: Runtime,
    device_id: int,
    template: dict[str, Any],
    lang: str = "en",
    *,
    budget_s: float = BUDGET_S,
    broadcast: bool = True,
    audit_ctx: Any = None,
) -> dict[str, Any]:
    """Create the template's missing sensors on a device. Returns ApplyTemplateResult.
    [2b-3] `audit_ctx` (AuditCtx): records `device.apply_template` in the same transaction.

    Applies to the same device are serialized (per-device lock), so concurrent apply-template /
    import calls cannot create duplicate sensors."""
    lock, users = _device_locks.get(device_id, (asyncio.Lock(), 0))
    _device_locks[device_id] = (lock, users + 1)
    try:
        async with lock:
            return await _apply_template(
                rt,
                device_id,
                template,
                lang,
                budget_s=budget_s,
                broadcast=broadcast,
                audit_ctx=audit_ctx,
            )
    finally:
        lock, users = _device_locks[device_id]
        if users <= 1:
            del _device_locks[device_id]
        else:
            _device_locks[device_id] = (lock, users - 1)


async def _apply_template(
    rt: Runtime,
    device_id: int,
    template: dict[str, Any],
    lang: str,
    *,
    budget_s: float,
    broadcast: bool,
    audit_ctx: Any = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    async with rt.sessionmaker() as session:
        device = await session.get(Device, device_id)
        if device is None:
            raise ApiError(404, "not_found", "Device not found")
        creds = await effective_credentials(session, device)
        sensors = (
            (await session.execute(select(Sensor).where(Sensor.device_id == device_id)))
            .scalars()
            .all()
        )
        existing = {k for s in sensors if (k := identity(s.type, s.config)) is not None}
        existing_names = {
            k: (s.id, s.name) for s in sensors if (k := identity(s.type, s.config)) is not None
        }
        next_sort = max((s.sort_order for s in sensors), default=-1) + 1
        default_interval = int(await settings_svc.get(session, "default_interval_s"))
        host, device_enabled = device.host, device.enabled
    ctx = _Ctx(
        host, creds, lang if lang in ("en", "he") else "en", existing, loop.time() + budget_s
    )
    ctx.probe_sem = asyncio.Semaphore(PROBES_PER_DEVICE)
    ctx.existing_names = existing_names

    slots: list[_Slot] = []
    for entry in template["entries"]:
        kind = entry["kind"]
        if _remaining(ctx) <= 0:
            if kind == "sensor":
                slots.append(
                    _Slot(
                        skip=_skip(
                            _l10n(entry["name"], ctx.lang),
                            entry["type"],
                            "probe_failed",
                            "Timed out",
                        )
                    )
                )
            else:
                t = "snmp_interface" if kind == "snmp_interfaces" else "snmp_custom"
                slots.append(_Slot(skip=_skip(kind, t, "probe_failed", "Timed out")))
            continue
        if kind == "sensor":
            slots.append(_sensor_candidate(entry, ctx))
        elif kind == "snmp_interfaces":
            slots.extend(await _interface_slots(rt, entry, ctx))
        elif kind == "snmp_table":
            slots.extend(await _table_slots(rt, entry, ctx))

    # [2b-2] order per entry: exists (above) -> when -> probe
    conditional = [s.candidate for s in slots if s.candidate is not None and s.candidate.when]
    if conditional:
        await asyncio.gather(*(_check_when(rt, ctx, c) for c in conditional))
    probes = [
        s.candidate
        for s in slots
        if s.candidate is not None and s.candidate.probe and s.candidate.skip is None
    ]
    if probes:
        await asyncio.gather(*(_run_probe(rt, ctx, c) for c in probes))

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    new_sensors: list[Sensor] = []
    seen = set(existing)
    async with rt.sessionmaker() as session:
        for slot in slots:
            if slot.skip is not None:
                skipped.append(slot.skip)
                continue
            cand = slot.candidate
            assert cand is not None
            if cand.skip is not None:
                skipped.append(cand.skip)
                continue
            key = identity(cand.type, cand.config)
            if key is not None and key in seen:
                skipped.append(_skip(cand.name, cand.type, "exists"))
                continue
            if key is not None:
                seen.add(key)
            s = Sensor(
                device_id=device_id,
                name=cand.name,
                type=cand.type,
                config=cand.config,
                interval_s=int(cand.interval_s or default_interval),
                enabled=True,
                status="unknown" if device_enabled else "paused",
                status_message="",
                state={},
                sort_order=next_sort,
                channels=build_channels(cand.type, cand.overrides, cand.config),
            )
            next_sort += 1
            session.add(s)
            new_sensors.append(s)
        renamed: list[int] = []
        for sensor_id, new_name, new_label in ctx.renames:
            row = await session.get(Sensor, sensor_id)
            if row is None:
                continue
            row.name = new_name
            if (row.config or {}).get("label") is not None:
                row.config = {**row.config, "label": new_label}
            renamed.append(sensor_id)
        if audit_ctx is not None and audit_ctx.enabled:
            from netmon.services import audit

            await session.flush()
            dev = await session.get(Device, device_id)
            await audit.record(
                session,
                audit_ctx,
                "device.apply_template",
                audit.entity("device", device_id, dev.name if dev else None, device_id=device_id),
                {
                    "template_id": [None, template["id"]],
                    "created": [None, [f"{s.name} ({s.type})" for s in new_sensors]],
                    "renamed": [None, renamed],
                },
            )
        await session.commit()
        for s in new_sensors:
            created.append({"sensor_id": s.id, "name": s.name, "type": s.type})
    if rt.scheduler:
        for s in new_sensors:
            await rt.scheduler.upsert(s.id)
        for sensor_id in renamed:
            await rt.scheduler.upsert(sensor_id)
    if broadcast:
        rt.config_changed("device", device_id, "updated")
    return {
        "device_id": device_id,
        "template_id": template["id"],
        "created": created,
        "skipped": skipped,
    }
