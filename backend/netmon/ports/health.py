"""[2b-2] Health strip (pure): sensor -> category, and one item per category
(docs/data-model.md "Health strip")."""

from __future__ import annotations

import re
from typing import Any

from netmon.poller.types import SEVERITY

CATEGORIES = ("cpu", "memory", "temperature", "poe", "psu", "fan")
_I = re.IGNORECASE
RE_POE = re.compile(r"^poe\b", _I)
RE_CPU = re.compile(r"\bcpu\b|מעבד", _I)
RE_MEM = re.compile(r"\bmem(ory)?\b|זיכרון", _I)
RE_TEMP = re.compile(r"temp|טמפרטורה", _I)
RE_FAN = re.compile(r"\bfans?\b|cooling|מאוורר", _I)
RE_PSU = re.compile(r"power supply|\bpsu\b|\bpower\b|ספק כוח", _I)
CELSIUS = ("°C", "C")


def _unit(sensor: dict[str, Any]) -> str:
    return str((sensor.get("config") or {}).get("unit") or "")


def categorize(sensor: dict[str, Any]) -> str | None:
    """`sensor`: {type, name, config}. First matching rule wins."""
    name = str(sensor.get("name") or "")
    type_ = sensor.get("type")
    custom = type_ == "snmp_custom"
    if RE_POE.search(name):
        return "poe"
    # temperature before cpu/memory: "Temperature CPU", "CPU temperature" are temperatures
    if custom and (RE_TEMP.search(name) or _unit(sensor) in CELSIUS):
        return "temperature"
    if type_ == "snmp_cpu" or (custom and RE_CPU.search(name)):
        return "cpu"
    if type_ == "snmp_memory" or (custom and RE_MEM.search(name)):
        return "memory"
    if custom and RE_FAN.search(name):
        return "fan"
    if custom and RE_PSU.search(name):
        return "psu"
    return None


def _primary_value(sensor: dict[str, Any]) -> float | None:
    v = sensor.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sensor_unit(sensor: dict[str, Any]) -> str:
    return str(sensor.get("unit") or "")


def health_items(sensors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`sensors`: {id, type, name, config, status, value (primary channel last_value), unit
    (primary channel unit)}. Returns HealthItem dicts in the fixed category order."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in sensors:
        cat = categorize(s)
        if cat is not None:
            groups.setdefault(cat, []).append(s)
    items = []
    for cat in CATEGORIES:
        members = sorted(groups.get(cat, []), key=lambda s: s["id"])
        if not members:
            continue
        active = [s for s in members if s["status"] != "paused"]
        status = (
            max((s["status"] for s in active), key=lambda st: SEVERITY.get(st, 0))
            if active
            else "paused"
        )
        value: float | None = None
        unit = ""
        if cat in ("cpu", "memory", "poe"):
            unit = "%"
            vals = [
                v
                for s in members
                if _sensor_unit(s) == "%" and (v := _primary_value(s)) is not None
            ]
            value = max(vals) if vals else None
        elif cat == "temperature":
            unit = "°C"
            vals = [
                v
                for s in members
                if _sensor_unit(s) in CELSIUS and (v := _primary_value(s)) is not None
            ]
            value = max(vals) if vals else None
        text = ""
        if cat in ("psu", "fan") or (cat == "temperature" and value is None):
            up = sum(1 for s in members if s["status"] == "up")
            text = f"{up}/{len(members)}"
        pool = active or members
        primary = min(
            pool,
            key=lambda s: (
                -SEVERITY.get(s["status"], 0),
                -(_primary_value(s) if _primary_value(s) is not None else float("-inf")),
                s["id"],
            ),
        )
        items.append(
            {
                "category": cat,
                "status": status,
                "value": value,
                "unit": unit,
                "text": text,
                "sensor_ids": [s["id"] for s in members],
                "primary_sensor_id": primary["id"],
            }
        )
    return items
