"""Apply a device template: create the missing sensors (idempotent).

Shared by `POST /api/devices/{id}/apply-template` and the discovery importer."""

from __future__ import annotations

import asyncio
import logging
import re
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
from netmon.poller.snmp_client import SnmpError, as_text, snmp_walk, walk_to_index
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
        return (type_, str(config.get("oid") or "").lstrip("."))
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


def _l10n(name: Any, lang: str) -> str:
    if isinstance(name, dict):
        return str(name.get(lang) or name.get("en") or "")
    return str(name or "")


def _skip(name: str, type_: str, reason: str, message: str = "") -> dict[str, Any]:
    return {"name": name[:128], "type": type_, "reason": reason, "message": message}


def _remaining(ctx: _Ctx) -> float:
    return ctx.deadline - asyncio.get_running_loop().time()


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
            type_, name, cfg, overrides, entry.get("interval_s"), bool(entry.get("probe"))
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
    except TimeoutError:
        return [_Slot(skip=_skip(label, "snmp_custom", "probe_failed", "Timed out"))]
    except SnmpError as exc:
        return [_Slot(skip=_skip(label, "snmp_custom", "snmp_error", str(exc)))]
    if not values:
        return [_Slot(skip=_skip(label, "snmp_custom", "no_rows", "The table has no rows"))]
    suffixes = sorted(values, key=_suffix_key)
    max_n = int(entry.get("max") or len(suffixes))
    overrides = validate_channel_overrides("snmp_custom", entry.get("channels") or [])
    slots = []
    for s in suffixes[:max_n]:
        text = as_text(labels.get(s)) if s in labels else ""
        text2 = as_text(labels2.get(s)) if s in labels2 else ""
        row_label = text or s
        if text and text2:
            row_label = f"{text} ({text2})"
        row_label = row_label[:64]
        cfg_in = {**(entry.get("config") or {}), "oid": f"{walk_oid}.{s}", "label": row_label}
        if entry.get("percent_of_column"):
            cfg_in["percent_of_oid"] = f"{str(entry['percent_of_column']).strip('.')}.{s}"
        cfg = validate_config("snmp_custom", cfg_in)
        name = name_tpl.replace("{label}", row_label).replace("{index}", s)[:128]
        if identity("snmp_custom", cfg) in ctx.existing:
            slots.append(_Slot(skip=_skip(name, "snmp_custom", "exists")))
            continue
        slots.append(_Slot(candidate=_Candidate("snmp_custom", name, cfg, overrides, None, False)))
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
) -> dict[str, Any]:
    """Create the template's missing sensors on a device. Returns ApplyTemplateResult.

    Applies to the same device are serialized (per-device lock), so concurrent apply-template /
    import calls cannot create duplicate sensors."""
    lock, users = _device_locks.get(device_id, (asyncio.Lock(), 0))
    _device_locks[device_id] = (lock, users + 1)
    try:
        async with lock:
            return await _apply_template(
                rt, device_id, template, lang, budget_s=budget_s, broadcast=broadcast
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
        next_sort = max((s.sort_order for s in sensors), default=-1) + 1
        default_interval = int(await settings_svc.get(session, "default_interval_s"))
        host, device_enabled = device.host, device.enabled
    ctx = _Ctx(
        host, creds, lang if lang in ("en", "he") else "en", existing, loop.time() + budget_s
    )
    ctx.probe_sem = asyncio.Semaphore(PROBES_PER_DEVICE)

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

    probes = [s.candidate for s in slots if s.candidate is not None and s.candidate.probe]
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
        await session.commit()
        for s in new_sensors:
            created.append({"sensor_id": s.id, "name": s.name, "type": s.type})
    if rt.scheduler:
        for s in new_sensors:
            await rt.scheduler.upsert(s.id)
    if broadcast:
        rt.config_changed("device", device_id, "updated")
    return {
        "device_id": device_id,
        "template_id": template["id"],
        "created": created,
        "skipped": skipped,
    }
