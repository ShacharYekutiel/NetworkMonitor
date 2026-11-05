"""[2b-2] Template library: schema validation of template data files and `load_dir()`.

Templates are YAML (`yaml.safe_load` only) or JSON files, one template per file named
`<id>.yaml`. See docs/data-model.md "Template library (data files)". Apart from reading the files
in `load_dir()`, everything here is pure."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from netmon.api.errors import ApiError

IF_EXCLUDE = (
    r"^(lo\d*|pflog\d*|pfsync\d*|enc\d*|gif\d*|gre\d*|ovpn[cs]?\d*|wg\d+|tun\d*|tap\d*|ipfw\d*"
    r"|docker\d*|veth.*|virbr.*|br-.*|dummy\d*|sit\d*|ifb\d*|imq\d*|teql\d*|ip6tnl\d*|wifi\d+"
    r"|ath\d+|ra\d+|rai\d+)$"
)
SOFTWARE_LOOPBACK = 24

SCHEMA_VERSION = 1
MAX_FILES = 200
MAX_FILE_BYTES = 256 * 1024
EXTENSIONS = (".yaml", ".yml", ".json")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
OID_RE = re.compile(r"^\.?[0-2](\.\d+){1,127}$")
VENDOR_PREFIX = "1.3.6.1.4.1."
DEVICE_CLASSES = (
    "router",
    "switch",
    "access_point",
    "printer",
    "nas",
    "server",
    "workstation",
    "unknown",
)
CONFIDENCES = ("high", "medium", "low")
OID_SENSOR_TYPES = ("snmp_custom", "snmp_walk_count")

# keys added in 2b-2 (removed by the equivalence test against the 2b-1 fixture)
TEMPLATE_KEYS_2B2 = (
    "version",
    "verified",
    "verified_note",
    "vendor",
    "source",
    "overrides_builtin",
    "file",
)
ENTRY_KEYS_2B2 = ("confidence", "notes", "when")
RULE_KEYS_2B2 = ("sys_object_id_prefixes", "sys_descr_regex")


class TemplateError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.message = message


def _join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


# ------------------------------------------------------------------ primitive checks
def _mapping(v: Any, path: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise TemplateError(path, "invalid value: must be a mapping")
    return v


def _keys(
    obj: dict[str, Any], path: str, allowed: tuple[str, ...], required: tuple[str, ...]
) -> None:
    for k in obj:
        if not isinstance(k, str) or k not in allowed:
            raise TemplateError(path, f"unknown key: {k}")
    for k in required:
        if k not in obj:
            raise TemplateError(path, f"missing key: {k}")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int(v: Any, path: str, lo: int | None = None, hi: int | None = None) -> int:
    if not _is_int(v):
        raise TemplateError(path, "invalid value: must be an integer")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        rng = f"{lo}-{hi}" if hi is not None else f">= {lo}"
        raise TemplateError(path, f"invalid value: must be {rng}")
    return int(v)


def _opt_int(v: Any, path: str, lo: int, hi: int) -> int | None:
    return None if v is None else _int(v, path, lo, hi)


def _bool(v: Any, path: str) -> bool:
    if not isinstance(v, bool):
        raise TemplateError(path, "invalid value: must be true or false")
    return v


def _str(v: Any, path: str, lo: int = 0, hi: int = 500) -> str:
    if not isinstance(v, str):
        raise TemplateError(path, "invalid value: must be a string")
    if not lo <= len(v) <= hi:
        raise TemplateError(path, f"invalid value: must be {lo}-{hi} characters")
    return v


def _opt_str(v: Any, path: str, hi: int = 500) -> str | None:
    return None if v is None else _str(v, path, 0, hi)


def _int_list(v: Any, path: str, lo: int | None = None, hi: int | None = None) -> list[int]:
    if not isinstance(v, list):
        raise TemplateError(path, "invalid value: must be a list of integers")
    return [_int(x, _join(path, i), lo, hi) for i, x in enumerate(v)]


def _opt_int_list(v: Any, path: str, lo: int | None = None, hi: int | None = None) -> list | None:
    return None if v is None else _int_list(v, path, lo, hi)


def _str_list(v: Any, path: str, hi: int = 64) -> list[str]:
    if not isinstance(v, list):
        raise TemplateError(path, "invalid value: must be a list of strings")
    return [_str(x, _join(path, i), 1, hi) for i, x in enumerate(v)]


def oid(v: Any, path: str) -> str:
    """A numeric OID string (leading dot stripped). YAML floats like 1.10 are rejected."""
    if not isinstance(v, str) or not OID_RE.match(v.strip()):
        raise TemplateError(path, f"invalid OID: {v}")
    return v.strip().lstrip(".")


def _opt_oid(v: Any, path: str) -> str | None:
    return None if v is None else oid(v, path)


def _regex(v: Any, path: str) -> str:
    if not isinstance(v, str):
        raise TemplateError(path, "invalid value: must be a string")
    try:
        re.compile(v)
    except re.error as exc:
        raise TemplateError(path, f"invalid regex: {exc}") from None
    return v


def _opt_regex(v: Any, path: str) -> str | None:
    return None if v is None else _regex(v, path)


def _l10n(v: Any, path: str, lo: int, hi: int) -> dict[str, str]:
    obj = _mapping(v, path)
    _keys(obj, path, ("en", "he"), ("en",))
    en = _str(obj["en"], _join(path, "en"), lo, hi)
    he = obj.get("he")
    he = en if he is None else _str(he, _join(path, "he"), lo, hi)
    return {"en": en, "he": he}


def _confidence(v: Any, path: str) -> str | None:
    if v is None:
        return None
    if v not in CONFIDENCES:
        raise TemplateError(path, "invalid value: must be high, medium, low or null")
    return str(v)


def _value_map_keys(config: dict[str, Any], path: str) -> dict[str, Any]:
    """value_map keys may be YAML ints or decimal strings: normalised to decimal strings."""
    vm = config.get("value_map")
    if vm is None or not isinstance(vm, dict):
        return config
    out: dict[str, Any] = {}
    for k, val in vm.items():
        if _is_int(k):
            key = str(k)
        elif isinstance(k, str) and re.fullmatch(r"-?\d{1,20}", k.strip()):
            key = str(int(k.strip()))
        else:
            raise TemplateError(_join(path, "value_map"), "value_map keys must be integers")
        out[key] = copy.deepcopy(val)
    return {**config, "value_map": out}


def _api_error_text(exc: ApiError) -> str:
    parts = []
    for d in exc.details or []:
        loc = [str(x) for x in (d.get("loc") or [])]
        while loc and loc[0] in ("body", "config", "channels"):
            loc = loc[1:]
        parts.append(f"{'.'.join(loc)}: {d.get('msg')}" if loc else str(d.get("msg")))
    return "; ".join(parts) or exc.message


def _validate_sensor_config(type_: str, config: dict[str, Any], path: str) -> None:
    from netmon.poller.sensor_types import validate_config

    try:
        validate_config(type_, config)
    except ApiError as exc:
        raise TemplateError(path, f"invalid config: {_api_error_text(exc)}") from None


def _validate_channels(type_: str, channels: Any, path: str) -> list[dict[str, Any]]:
    from netmon.poller.sensor_types import validate_channel_overrides

    if not isinstance(channels, list):
        raise TemplateError(path, "invalid channels: must be a list")
    try:
        validate_channel_overrides(type_, channels)
    except ApiError as exc:
        raise TemplateError(path, f"invalid channels: {_api_error_text(exc)}") from None
    return copy.deepcopy(channels)


def _filters(v: Any, path: str) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        raise TemplateError(path, "invalid value: must be a list")
    out = []
    for i, f in enumerate(v):
        fp = _join(path, i)
        obj = _mapping(f, fp)
        _keys(obj, fp, ("oid", "in", "not_in"), ("oid",))
        out.append(
            {
                "oid": oid(obj["oid"], _join(fp, "oid")),
                "in": _opt_int_list(obj.get("in"), _join(fp, "in")),
                "not_in": _opt_int_list(obj.get("not_in"), _join(fp, "not_in")),
            }
        )
    return out


def _vendor_oid(*oids: Any) -> bool:
    return any(isinstance(o, str) and o.lstrip(".").startswith(VENDOR_PREFIX) for o in oids)


# ------------------------------------------------------------------ entries
COMMON_ENTRY = ("kind", "confidence", "notes")
SENSOR_KEYS = (*COMMON_ENTRY, "type", "name", "config", "channels", "interval_s", "probe", "when")
INTERFACES_KEYS = (
    *COMMON_ENTRY,
    "oper_up_only",
    "exclude_if_types",
    "include_name_regex",
    "exclude_name_regex",
    "max",
    "channels",
)
TABLE_KEYS = (
    *COMMON_ENTRY,
    "walk_oid",
    "name",
    "config",
    "label_oid",
    "label2_oid",
    "percent_of_column",
    "channels",
    "max",
    "skip_zero",
    "filters",
    "interval_s",
)
TABLE_FORBIDDEN_CONFIG = ("oid", "percent_of_oid", "label")


def _sensor_entry(e: dict[str, Any], path: str) -> dict[str, Any]:
    from netmon.poller.sensor_types import SENSOR_TYPES

    _keys(e, path, SENSOR_KEYS, ("kind", "type", "name"))
    type_ = e["type"]
    if not isinstance(type_, str) or type_ not in SENSOR_TYPES:
        raise TemplateError(_join(path, "type"), f"unknown sensor type: {type_}")
    name = _l10n(e["name"], _join(path, "name"), 1, 128)
    config = _mapping(e.get("config", {}), _join(path, "config"))
    cpath = _join(path, "config")
    if type_ in OID_SENSOR_TYPES:
        for key in ("oid", "percent_of_oid"):
            if config.get(key) is not None:
                oid(config[key], _join(cpath, key))
    config = _value_map_keys(dict(config), cpath)
    _validate_sensor_config(type_, config, cpath)
    channels = _validate_channels(type_, e.get("channels", []), _join(path, "channels"))
    out = {
        "kind": "sensor",
        "type": type_,
        "name": name,
        "config": copy.deepcopy(config),
        "channels": channels,
        "interval_s": _opt_int(e.get("interval_s"), _join(path, "interval_s"), 10, 86400),
        "probe": _bool(e.get("probe", False), _join(path, "probe")),
        "when": _filters(e.get("when", []), _join(path, "when")),
    }
    vendor = type_ in OID_SENSOR_TYPES and _vendor_oid(
        config.get("oid"), config.get("percent_of_oid")
    )
    return _common(out, e, path, vendor)


def _interfaces_entry(e: dict[str, Any], path: str) -> dict[str, Any]:
    _keys(e, path, INTERFACES_KEYS, ("kind", "max"))
    out = {
        "kind": "snmp_interfaces",
        "oper_up_only": _bool(e.get("oper_up_only", True), _join(path, "oper_up_only")),
        "exclude_if_types": _int_list(
            e.get("exclude_if_types", [SOFTWARE_LOOPBACK]), _join(path, "exclude_if_types"), 0
        ),
        "include_name_regex": _opt_regex(
            e.get("include_name_regex"), _join(path, "include_name_regex")
        ),
        # absent -> IF_EXCLUDE, explicit null -> no exclusion
        "exclude_name_regex": _opt_regex(
            e.get("exclude_name_regex", IF_EXCLUDE), _join(path, "exclude_name_regex")
        ),
        "max": _int(e["max"], _join(path, "max"), 1, 1000),
        "channels": _validate_channels(
            "snmp_interface", e.get("channels", []), _join(path, "channels")
        ),
    }
    return _common(out, e, path, False)


def _table_entry(e: dict[str, Any], path: str) -> dict[str, Any]:
    _keys(e, path, TABLE_KEYS, ("kind", "walk_oid", "name", "config"))
    walk = oid(e["walk_oid"], _join(path, "walk_oid"))
    label_oid = _opt_oid(e.get("label_oid"), _join(path, "label_oid"))
    label2_oid = _opt_oid(e.get("label2_oid"), _join(path, "label2_oid"))
    pct = _opt_oid(e.get("percent_of_column"), _join(path, "percent_of_column"))
    cpath = _join(path, "config")
    config = _mapping(e["config"], cpath)
    for key in TABLE_FORBIDDEN_CONFIG:
        if key in config:
            raise TemplateError(_join(cpath, key), f"invalid config: {key} is set by the table")
    config = _value_map_keys(dict(config), cpath)
    probe_cfg = {**config, "oid": "1.3.6.1"}
    if pct is not None:
        probe_cfg["percent_of_oid"] = "1.3.6.1"
    _validate_sensor_config("snmp_custom", probe_cfg, cpath)
    out = {
        "kind": "snmp_table",
        "walk_oid": walk,
        "label_oid": label_oid,
        "label2_oid": label2_oid,
        "percent_of_column": pct,
        "name": _l10n(e["name"], _join(path, "name"), 1, 128),
        "config": copy.deepcopy(config),
        "channels": _validate_channels(
            "snmp_custom", e.get("channels", []), _join(path, "channels")
        ),
        "max": _int(e.get("max", 8), _join(path, "max"), 1, 1000),
        "skip_zero": _bool(e.get("skip_zero", False), _join(path, "skip_zero")),
        "filters": _filters(e.get("filters", []), _join(path, "filters")),
        "interval_s": _opt_int(e.get("interval_s"), _join(path, "interval_s"), 10, 86400),
    }
    return _common(out, e, path, _vendor_oid(walk))


def _common(out: dict[str, Any], e: dict[str, Any], path: str, vendor: bool) -> dict[str, Any]:
    out["confidence"] = _confidence(e.get("confidence"), _join(path, "confidence"))
    out["notes"] = _str(e.get("notes", ""), _join(path, "notes"), 0, 500)
    if vendor and out["confidence"] is None:
        raise TemplateError(path, "confidence required for vendor OIDs")
    return out


ENTRY_KINDS = {
    "sensor": _sensor_entry,
    "snmp_interfaces": _interfaces_entry,
    "snmp_table": _table_entry,
}


def _entry(e: Any, path: str) -> dict[str, Any]:
    obj = _mapping(e, path)
    kind = obj.get("kind")
    if kind is None:
        raise TemplateError(path, "missing key: kind")
    if kind not in ENTRY_KINDS:
        raise TemplateError(
            _join(path, "kind"), "invalid value: kind must be sensor, snmp_interfaces or snmp_table"
        )
    return ENTRY_KINDS[kind](obj, path)


# ------------------------------------------------------------------ rules / template
RULE_KEYS = (
    "device_classes",
    "vendors",
    "requires_snmp",
    "any_open_ports",
    "enterprises",
    "sys_object_id_prefixes",
    "sys_descr_regex",
)


def _rule(r: Any, path: str) -> dict[str, Any]:
    obj = _mapping(r, path)
    _keys(obj, path, RULE_KEYS, ())
    classes = obj.get("device_classes")
    if classes is not None:
        classes = _str_list(classes, _join(path, "device_classes"))
        for i, c in enumerate(classes):
            if c not in DEVICE_CLASSES:
                raise TemplateError(
                    _join(_join(path, "device_classes"), i), f"invalid value: unknown class {c}"
                )
    prefixes = obj.get("sys_object_id_prefixes")
    if prefixes is not None:
        ppath = _join(path, "sys_object_id_prefixes")
        if not isinstance(prefixes, list):
            raise TemplateError(ppath, "invalid value: must be a list of OIDs")
        prefixes = [oid(p, _join(ppath, i)) for i, p in enumerate(prefixes)]
    vendors = obj.get("vendors")
    return {
        "device_classes": classes,
        "vendors": None if vendors is None else _str_list(vendors, _join(path, "vendors")),
        "requires_snmp": _bool(obj.get("requires_snmp", False), _join(path, "requires_snmp")),
        "any_open_ports": _opt_int_list(
            obj.get("any_open_ports"), _join(path, "any_open_ports"), 1, 65535
        ),
        "enterprises": _opt_int_list(obj.get("enterprises"), _join(path, "enterprises"), 0),
        "sys_object_id_prefixes": prefixes,
        "sys_descr_regex": _opt_regex(obj.get("sys_descr_regex"), _join(path, "sys_descr_regex")),
    }


TOP_KEYS = (
    "schema",
    "id",
    "version",
    "name",
    "description",
    "vendor",
    "verified",
    "verified_note",
    "requires_snmp",
    "priority",
    "match",
    "entries",
)


def _list(v: Any, path: str, lo: int, hi: int) -> list[Any]:
    if not isinstance(v, list) or not lo <= len(v) <= hi:
        raise TemplateError(path, f"invalid value: must be a list of {lo}-{hi} items")
    return v


def validate_template(data: Any, stem: str) -> dict[str, Any]:
    """Validate one parsed file. Returns the normalised template dict (every key present,
    without the source keys); raises TemplateError on the first problem."""
    if not isinstance(data, dict):
        raise TemplateError("", "not a mapping")
    if "schema" not in data:
        raise TemplateError("", "missing key: schema")
    if data["schema"] != SCHEMA_VERSION or isinstance(data["schema"], bool):
        raise TemplateError("schema", f"unsupported schema version: {data['schema']}")
    _keys(data, "", TOP_KEYS, ("id", "name", "priority", "match", "entries"))
    tid = data["id"]
    if not isinstance(tid, str) or not ID_RE.match(tid):
        raise TemplateError("id", "invalid value: id must match ^[a-z][a-z0-9_]{1,31}$")
    if tid != stem:
        raise TemplateError("id", f'id must match the file name (expected "{stem}")')
    desc = data.get("description")
    description = {"en": "", "he": ""} if desc is None else _l10n(desc, "description", 0, 500)
    out: dict[str, Any] = {
        "id": tid,
        "name": _l10n(data["name"], "name", 1, 128),
        "description": description,
        "requires_snmp": _bool(data.get("requires_snmp", False), "requires_snmp"),
        "priority": _int(data["priority"], "priority", 0, 1000),
        "match": [
            _rule(r, _join("match", i)) for i, r in enumerate(_list(data["match"], "match", 1, 20))
        ],
        "entries": [
            _entry(e, _join("entries", i))
            for i, e in enumerate(_list(data["entries"], "entries", 1, 100))
        ],
        "version": _int(data.get("version", 1), "version", 1),
        "verified": _bool(data.get("verified", False), "verified"),
        "verified_note": _str(data.get("verified_note", ""), "verified_note", 0, 200),
        "vendor": _opt_str(data.get("vendor"), "vendor", 64),
    }
    return copy.deepcopy(out)


# ------------------------------------------------------------------ files
def parse_text(text: str, suffix: str) -> Any:
    """Parse file text (YAML with safe_load, or JSON). Raises TemplateError on syntax errors."""
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise TemplateError("", f"invalid JSON: {exc}") from None
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = " ".join(str(exc).split())
        raise TemplateError("", f"invalid YAML: {msg}") from None


def _error(source: str, file: str, tid: str | None, path: str, message: str) -> dict[str, Any]:
    return {"source": source, "file": file, "template_id": tid, "path": path, "message": message}


def template_files(root: Path) -> list[tuple[str, Path]]:
    """(relative path with forward slashes, path) of every template file under root, sorted."""
    if not root.is_dir():
        return []
    found = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in EXTENSIONS:
            found.append((p.relative_to(root).as_posix(), p))
    return sorted(found, key=lambda x: x[0])


def load_dir(root: Path, source: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load every template file under `root` (recursively). Returns (templates, errors). Any
    error rejects the whole file; other files still load. Templates keep file order."""
    templates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    files = template_files(root)
    if len(files) > MAX_FILES:
        errors.append(_error(source, files[MAX_FILES][0], None, "", "too many files"))
        files = files[:MAX_FILES]
    for rel, path in files:
        stem = path.name[: -len(path.suffix)]
        tid: str | None = None
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise TemplateError("", "file too large")
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise TemplateError("", "invalid value: the file is not UTF-8") from None
            data = parse_text(text, path.suffix.lower())
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                tid = data["id"]
            t = validate_template(data, stem)
            if t["id"] in seen:
                raise TemplateError("", f'duplicate id "{t["id"]}" (also in {seen[t["id"]]})')
        except TemplateError as exc:
            errors.append(_error(source, rel, tid, exc.path, exc.message))
            continue
        except OSError as exc:
            errors.append(_error(source, rel, tid, "", f"invalid value: cannot read ({exc})"))
            continue
        seen[t["id"]] = rel
        t["source"] = source
        t["overrides_builtin"] = False
        t["file"] = rel
        templates.append(t)
    return templates, errors


def strip_2b2(template: dict[str, Any]) -> dict[str, Any]:
    """The template without the keys added in 2b-2 (for the 2b-1 equivalence test)."""
    t = {k: copy.deepcopy(v) for k, v in template.items() if k not in TEMPLATE_KEYS_2B2}
    t["match"] = [{k: v for k, v in r.items() if k not in RULE_KEYS_2B2} for r in t["match"]]
    t["entries"] = [{k: v for k, v in e.items() if k not in ENTRY_KEYS_2B2} for e in t["entries"]]
    return t
