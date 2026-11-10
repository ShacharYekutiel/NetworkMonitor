"""Sensor type definitions: config schemas and channels. Source of truth in code; must match
docs/data-model.md "Sensor types: config and channels"."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from netmon.api.errors import ApiError

SENSOR_TYPES_ORDER = [
    "ping",
    "snmp_system",
    "snmp_cpu",
    "snmp_memory",
    "snmp_interface",
    "http",
    "tcp_port",
    "dns",
    "snmp_custom",
    "snmp_disk",
    "snmp_walk_count",  # [2b-2]
    "trap",  # [2b-3] passive event sensors
    "syslog",
    "port_flap",
    "config_backup",  # [3] passive event sensors
    "new_mac",
]
PASSIVE_TYPES = ("trap", "syslog", "port_flap", "config_backup", "new_mac")
AUTO_SENSOR_TYPES = {"ping", "snmp_system", "snmp_cpu", "snmp_memory"}


@dataclass(frozen=True)
class ConfigField:
    key: str
    # int | str | bool | enum | int_list | str_nullable | int_nullable | headers | float
    # | value_map ([2b-1])
    type: str
    required: bool = False
    default: Any = None
    min: float | None = None
    max: float | None = None
    options: list[str] | None = None


@dataclass(frozen=True)
class ChannelDef:
    key: str
    unit: str
    warning_min: float | None = None
    warning_max: float | None = None
    down_min: float | None = None
    down_max: float | None = None


@dataclass(frozen=True)
class SensorTypeDef:
    type: str
    requires_snmp: bool
    primary_channel: str
    config_fields: list[ConfigField] = field(default_factory=list)
    channels: list[ChannelDef] = field(default_factory=list)
    # [2b-3] passive: not scheduled, no measurements, status driven by event conditions
    passive: bool = False

    def channel(self, key: str) -> ChannelDef | None:
        for ch in self.channels:
            if ch.key == key:
                return ch
        return None

    def to_api(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "requires_snmp": self.requires_snmp,
            "primary_channel": self.primary_channel,
            "config_fields": [asdict(f) for f in self.config_fields],
            "channels": [asdict(c) for c in self.channels],
            "passive": self.passive,
        }


SENSOR_TYPES: dict[str, SensorTypeDef] = {
    "ping": SensorTypeDef(
        "ping",
        False,
        "latency_ms",
        [
            ConfigField("count", "int", default=4, min=1, max=20),
            ConfigField("timeout_ms", "int", default=1000, min=100, max=10000),
            ConfigField("packet_size", "int", default=56, min=16, max=1472),
        ],
        [
            ChannelDef("latency_ms", "ms", warning_max=100, down_max=500),
            ChannelDef("packet_loss_pct", "%", warning_max=10, down_max=50),
        ],
    ),
    "snmp_system": SensorTypeDef(
        "snmp_system", True, "uptime_s", [], [ChannelDef("uptime_s", "s")]
    ),
    "snmp_cpu": SensorTypeDef(
        "snmp_cpu",
        True,
        "cpu_load_pct",
        [],
        [ChannelDef("cpu_load_pct", "%", warning_max=80, down_max=95)],
    ),
    "snmp_memory": SensorTypeDef(
        "snmp_memory",
        True,
        "mem_used_pct",
        [
            ConfigField("storage_index", "int_nullable", default=None, min=1),
            ConfigField("storage_descr", "str", default=""),
        ],
        [
            ChannelDef("mem_used_pct", "%", warning_max=85, down_max=95),
            ChannelDef("mem_used_bytes", "B"),
            ChannelDef("mem_total_bytes", "B"),
        ],
    ),
    "snmp_interface": SensorTypeDef(
        "snmp_interface",
        True,
        "in_bps",
        [
            ConfigField("if_index", "int", required=True, min=1),
            ConfigField("if_name", "str", default=""),
            ConfigField("speed_bps_override", "int_nullable", default=None, min=1),
        ],
        [
            ChannelDef("in_bps", "bit/s"),
            ChannelDef("out_bps", "bit/s"),
            ChannelDef("in_util_pct", "%"),
            ChannelDef("out_util_pct", "%"),
            ChannelDef("oper_status", "code", down_max=1),
            ChannelDef("in_errors_ps", "/s"),
            ChannelDef("out_errors_ps", "/s"),
        ],
    ),
    "http": SensorTypeDef(
        "http",
        False,
        "response_time_ms",
        [
            ConfigField("url", "str", required=True),
            ConfigField("method", "enum", default="GET", options=["GET", "HEAD"]),
            ConfigField("timeout_s", "int", default=10, min=1, max=60),
            ConfigField("expected_status", "int_list", default=None, min=100, max=599),
            ConfigField("keyword", "str_nullable", default=None),
            ConfigField("keyword_must_exist", "bool", default=True),
            ConfigField("verify_tls", "bool", default=True),
            ConfigField("follow_redirects", "bool", default=True),
            ConfigField("headers", "headers", default={}),
        ],
        [
            ChannelDef("response_time_ms", "ms", warning_max=1000, down_max=5000),
            ChannelDef("status_code", "code"),
            ChannelDef("ssl_days_left", "d", warning_min=14, down_min=3),
        ],
    ),
    "tcp_port": SensorTypeDef(
        "tcp_port",
        False,
        "connect_time_ms",
        [
            ConfigField("port", "int", required=True, min=1, max=65535),
            ConfigField("timeout_s", "int", default=5, min=1, max=60),
        ],
        [ChannelDef("connect_time_ms", "ms", warning_max=500, down_max=2000)],
    ),
    "dns": SensorTypeDef(
        "dns",
        False,
        "resolve_time_ms",
        [
            ConfigField("query_name", "str", required=True),
            ConfigField(
                "record_type",
                "enum",
                default="A",
                options=["A", "AAAA", "CNAME", "MX", "NS", "TXT", "PTR"],
            ),
            ConfigField("server", "str_nullable", default=None),
            ConfigField("port", "int", default=53, min=1, max=65535),
            ConfigField("timeout_s", "int", default=5, min=1, max=30),
            ConfigField("expected", "str_nullable", default=None),
        ],
        [ChannelDef("resolve_time_ms", "ms", warning_max=200, down_max=1000)],
    ),
    "snmp_custom": SensorTypeDef(
        "snmp_custom",
        True,
        "value",
        [
            ConfigField("oid", "str", required=True),
            ConfigField(
                "value_type",
                "enum",
                default="gauge",
                options=["gauge", "counter", "string", "enum"],
            ),
            ConfigField("unit", "str", default=""),
            ConfigField("multiplier", "float", default=1.0, min=-1e12, max=1e12),
            ConfigField("label", "str", default=""),
            ConfigField("percent_of_oid", "str_nullable", default=None),
            ConfigField("expected", "str_nullable", default=None),
            ConfigField("match", "enum", default="equals", options=["equals", "contains"]),
            # [2b-1] value_type enum
            ConfigField("value_map", "value_map", default={}),
            ConfigField(
                "value_map_default", "enum", default="warning", options=["up", "warning", "down"]
            ),
            # [2b-2] "sum": value = raw / (raw + other) x 100 (percent_of_oid = the other part)
            ConfigField("percent_base", "enum", default="total", options=["total", "sum"]),
        ],
        [ChannelDef("value", "")],
    ),
    "snmp_disk": SensorTypeDef(
        "snmp_disk",
        True,
        "disk_used_pct",
        [
            ConfigField("storage_descr", "str", required=True),
            ConfigField("storage_index", "int_nullable", default=None, min=1),
        ],
        [
            ChannelDef("disk_used_pct", "%", warning_max=85, down_max=95),
            ChannelDef("disk_used_bytes", "B"),
            ChannelDef("disk_free_bytes", "B"),
            ChannelDef("disk_total_bytes", "B"),
        ],
    ),
    # [2b-2] rows of one table column, and the rows whose integer value is in a set
    "snmp_walk_count": SensorTypeDef(
        "snmp_walk_count",
        True,
        "matched",
        [
            ConfigField("oid", "str", required=True),
            ConfigField("count_values", "int_list", default=None),
            ConfigField("label", "str", default=""),
            ConfigField("max_rows", "int", default=5000, min=1, max=10000),
        ],
        [ChannelDef("rows", "#"), ChannelDef("matched", "#"), ChannelDef("unmatched", "#")],
    ),
    # [2b-3] passive event sensors: one channel `active` (active conditions, no thresholds)
    **{
        t: SensorTypeDef(t, False, "active", [], [ChannelDef("active", "#")], passive=True)
        for t in PASSIVE_TYPES
    },
}

# [2b-2] optional keys that are stored only when given (absent = the default); keeps the
# stored config of sensors created before the key existed unchanged
OMIT_WHEN_ABSENT = {("snmp_custom", "percent_base")}

OID_RE = re.compile(r"^\.?[0-2](\.\d+){1,127}$")


def normalize_oid(v: str) -> str:
    return v.strip().lstrip(".")


def is_passive(type_: str) -> bool:
    """[2b-3] passive event sensor types (never scheduled or run)."""
    return type_ in PASSIVE_TYPES


def get_type(type_: str) -> SensorTypeDef:
    t = SENSOR_TYPES.get(type_)
    if t is None:
        raise ApiError(400, "invalid_sensor_type", f"Unsupported sensor type: {type_!r}")
    return t


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() and abs(v) < 1e15 else f"{v:g}"


def _check_range(f: ConfigField, v: float, loc: list, errors: list) -> None:
    if f.min is not None and v < f.min:
        errors.append({"loc": loc, "msg": f"must be >= {_num(f.min)}"})
    if f.max is not None and v > f.max:
        errors.append({"loc": loc, "msg": f"must be <= {_num(f.max)}"})


_HTTP_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)


def validate_config(
    type_: str, config: Any, base: dict[str, Any] | None = None, loc_prefix: str = "config"
) -> dict[str, Any]:
    """Validate a config dict (optionally merged over `base`) and return the complete config.

    Raises ApiError(422) listing every problem.
    """
    tdef = get_type(type_)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ApiError(
            422,
            "validation_error",
            "config must be an object",
            [{"loc": ["body", loc_prefix], "msg": "must be an object"}],
        )
    known = {f.key: f for f in tdef.config_fields}
    errors: list[dict[str, Any]] = []
    for key in config:
        if key not in known:
            errors.append({"loc": ["body", loc_prefix, key], "msg": "unknown config key"})
    merged: dict[str, Any] = dict(base or {})
    merged.update(config)
    out: dict[str, Any] = {}
    for f in tdef.config_fields:
        loc = ["body", loc_prefix, f.key]
        if f.key not in merged or (merged[f.key] is None and f.required):
            if f.required:
                errors.append({"loc": loc, "msg": "field required"})
                continue
            if (type_, f.key) in OMIT_WHEN_ABSENT:
                continue
            out[f.key] = _copy_default(f.default)
            continue
        v = merged[f.key]
        if f.type == "int":
            if not _is_int(v):
                errors.append({"loc": loc, "msg": "must be an integer"})
                continue
            _check_range(f, v, loc, errors)
        elif f.type == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append({"loc": loc, "msg": "must be a number"})
                continue
            v = float(v)
            _check_range(f, v, loc, errors)
        elif f.type == "int_nullable":
            if v is not None:
                if not _is_int(v):
                    errors.append({"loc": loc, "msg": "must be an integer or null"})
                    continue
                _check_range(f, v, loc, errors)
        elif f.type == "str":
            if not isinstance(v, str):
                errors.append({"loc": loc, "msg": "must be a string"})
                continue
            if f.required and not v.strip():
                errors.append({"loc": loc, "msg": "must not be empty"})
                continue
            if len(v) > 2048:
                errors.append({"loc": loc, "msg": "too long"})
        elif f.type == "str_nullable":
            if v is not None and not isinstance(v, str):
                errors.append({"loc": loc, "msg": "must be a string or null"})
                continue
            if isinstance(v, str) and v == "":
                v = None
        elif f.type == "bool":
            if not isinstance(v, bool):
                errors.append({"loc": loc, "msg": "must be a boolean"})
                continue
        elif f.type == "enum":
            if v not in (f.options or []):
                errors.append({"loc": loc, "msg": f"must be one of {f.options}"})
                continue
        elif f.type == "int_list":
            if v is not None:
                if not isinstance(v, list) or not v or not all(_is_int(x) for x in v):
                    errors.append({"loc": loc, "msg": "must be a non-empty list of integers"})
                    continue
                for x in v:
                    _check_range(f, x, loc, errors)
        elif f.type == "value_map":
            v = _validate_value_map(v, loc, errors)
            if v is None:
                continue
        elif f.type == "headers":
            if not isinstance(v, dict) or not all(
                isinstance(k, str) and isinstance(x, str) for k, x in v.items()
            ):
                errors.append({"loc": loc, "msg": "must be an object of string to string"})
                continue
        out[f.key] = v
    # type specific checks
    if type_ == "http" and isinstance(out.get("url"), str):
        if not _HTTP_URL.match(out["url"]):
            errors.append({"loc": ["body", loc_prefix, "url"], "msg": "must start with http(s)://"})
    if type_ == "dns" and isinstance(out.get("query_name"), str) and len(out["query_name"]) > 255:
        errors.append({"loc": ["body", loc_prefix, "query_name"], "msg": "too long"})
    if type_ == "snmp_custom":
        _validate_snmp_custom(out, loc_prefix, errors)
    if type_ == "snmp_walk_count":
        _validate_walk_count(out, loc_prefix, errors)
    if type_ == "snmp_disk" and isinstance(out.get("storage_descr"), str):
        if len(out["storage_descr"]) > 255:
            errors.append(
                {"loc": ["body", loc_prefix, "storage_descr"], "msg": "must be 1-255 characters"}
            )
    if errors:
        raise ApiError(422, "validation_error", "Invalid sensor config", errors)
    return out


def _validate_snmp_custom(out: dict[str, Any], loc_prefix: str, errors: list) -> None:
    for key in ("oid", "percent_of_oid"):
        v = out.get(key)
        if not isinstance(v, str):
            continue
        if not OID_RE.match(v.strip()):
            errors.append(
                {"loc": ["body", loc_prefix, key], "msg": "must be a numeric OID like 1.3.6.1..."}
            )
        else:
            out[key] = normalize_oid(v)
    if isinstance(out.get("unit"), str) and len(out["unit"]) > 16:
        errors.append({"loc": ["body", loc_prefix, "unit"], "msg": "must be 0-16 characters"})
    if isinstance(out.get("label"), str) and len(out["label"]) > 64:
        errors.append({"loc": ["body", loc_prefix, "label"], "msg": "must be 0-64 characters"})
    if out.get("multiplier") == 0:
        errors.append({"loc": ["body", loc_prefix, "multiplier"], "msg": "must not be 0"})
    if out.get("value_type") == "enum" and not out.get("value_map"):
        errors.append(
            {
                "loc": ["body", loc_prefix, "value_map"],
                "msg": "required (non-empty) for value_type enum",
            }
        )
    if out.get("percent_of_oid") is not None and out.get("value_type") != "gauge":
        errors.append(
            {
                "loc": ["body", loc_prefix, "percent_of_oid"],
                "msg": "only allowed when value_type is gauge",
            }
        )
    if out.get("percent_base") == "sum" and out.get("percent_of_oid") is None:
        errors.append(
            {
                "loc": ["body", loc_prefix, "percent_base"],
                "msg": '"sum" requires percent_of_oid',
            }
        )


def _validate_walk_count(out: dict[str, Any], loc_prefix: str, errors: list) -> None:
    v = out.get("oid")
    if isinstance(v, str):
        if not OID_RE.match(v.strip()):
            errors.append(
                {"loc": ["body", loc_prefix, "oid"], "msg": "must be a numeric OID like 1.3.6.1..."}
            )
        else:
            out["oid"] = normalize_oid(v)
    if isinstance(out.get("label"), str) and len(out["label"]) > 64:
        errors.append({"loc": ["body", loc_prefix, "label"], "msg": "must be 0-64 characters"})


VALUE_MAP_MAX = 32
_INT_KEY = re.compile(r"^-?\d{1,20}$")


def _validate_value_map(v: Any, loc: list, errors: list) -> dict[str, Any] | None:
    """{"<integer>": {"status": up|warning|down, "text": str (0-32)}}, at most 32 entries."""
    if not isinstance(v, dict):
        errors.append({"loc": loc, "msg": "must be an object"})
        return None
    if len(v) > VALUE_MAP_MAX:
        errors.append({"loc": loc, "msg": f"at most {VALUE_MAP_MAX} entries"})
        return None
    out: dict[str, Any] = {}
    for key, entry in v.items():
        eloc = [*loc, key]
        if not isinstance(key, str) or not _INT_KEY.match(key):
            errors.append({"loc": eloc, "msg": "keys must be integers as decimal strings"})
            continue
        if not isinstance(entry, dict) or set(entry) - {"status", "text"}:
            errors.append({"loc": eloc, "msg": "must be an object with status and text"})
            continue
        if entry.get("status") not in ("up", "warning", "down"):
            errors.append({"loc": [*eloc, "status"], "msg": "must be one of up, warning, down"})
            continue
        txt = entry.get("text", "")
        if not isinstance(txt, str) or len(txt) > 32:
            errors.append({"loc": [*eloc, "text"], "msg": "must be a string of 0-32 characters"})
            continue
        out[str(int(key))] = {"status": entry["status"], "text": txt}
    return out


def _copy_default(v: Any) -> Any:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, list):
        return list(v)
    return v


THRESHOLD_KEYS = ("warning_min", "warning_max", "down_min", "down_max")


def channel_unit(type_: str, ch: ChannelDef, config: dict[str, Any] | None) -> str:
    """The unit stored on a sensor channel: snmp_custom takes it from config.unit."""
    if type_ == "snmp_custom" and ch.key == "value" and config is not None:
        return str(config.get("unit") or "")[:16]
    return ch.unit


def validate_channel_overrides(type_: str, channels: Any) -> dict[str, dict[str, float | None]]:
    """Returns {key: {threshold_key: value}} containing only keys that were present."""
    tdef = get_type(type_)
    if channels is None:
        return {}
    errors: list[dict[str, Any]] = []
    result: dict[str, dict[str, float | None]] = {}
    if not isinstance(channels, list):
        raise ApiError(
            422,
            "validation_error",
            "channels must be a list",
            [{"loc": ["body", "channels"], "msg": "must be a list"}],
        )
    for i, entry in enumerate(channels):
        loc: list[Any] = ["body", "channels", i]
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            errors.append({"loc": loc, "msg": "must be an object with a key"})
            continue
        key = entry["key"]
        if tdef.channel(key) is None:
            errors.append({"loc": [*loc, "key"], "msg": f"unknown channel {key!r}"})
            continue
        overrides: dict[str, float | None] = {}
        for k, v in entry.items():
            if k == "key":
                continue
            if k not in THRESHOLD_KEYS:
                errors.append({"loc": [*loc, k], "msg": "unknown field"})
                continue
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                errors.append({"loc": [*loc, k], "msg": "must be a number or null"})
                continue
            overrides[k] = float(v) if v is not None else None
        result.setdefault(key, {}).update(overrides)
    if errors:
        raise ApiError(422, "validation_error", "Invalid channel thresholds", errors)
    return result


DEFAULT_NAMES = {
    "ping": "Ping",
    "snmp_system": "System",
    "snmp_cpu": "CPU Load",
    "snmp_memory": "Memory",
    "snmp_interface": "Interface",
    "http": "HTTP",
    "tcp_port": "TCP Port",
    "dns": "DNS",
    "snmp_custom": "SNMP",
    "snmp_disk": "Disk",
    "snmp_walk_count": "Count",
    "trap": "SNMP traps",  # [2b-3]
    "syslog": "Syslog",
    "port_flap": "Port flapping",
    "config_backup": "Configuration backup",  # [3]
    "new_mac": "New devices",
}


def default_sensor_name(type_: str, config: dict[str, Any]) -> str:
    """English display name used when the client sends no name (and for auto_sensors)."""
    if type_ == "snmp_interface" and config.get("if_name"):
        return str(config["if_name"])[:128]
    if type_ == "tcp_port" and config.get("port"):
        return f"TCP {config['port']}"
    if type_ == "snmp_custom":
        if config.get("label"):
            return str(config["label"])[:128]
        return f"SNMP {config.get('oid', '')}"[:128]
    if type_ == "snmp_disk" and config.get("storage_descr"):
        return f"Disk {config['storage_descr']}"[:128]
    if type_ == "snmp_walk_count":
        return f"Count {config.get('oid', '')}"[:128]
    return DEFAULT_NAMES.get(type_, type_)
