"""SNMP enumeration shared by the API (interface / storage pickers) and the templates."""

from __future__ import annotations

from typing import Any

from netmon.poller.snmp_client import SnmpError, as_text, snmp_walk, walk_to_index

IF_STATUS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}
IF_TABLE = "1.3.6.1.2.1.2.2.1"
IF_XTABLE = "1.3.6.1.2.1.31.1.1.1"


def _mac(v: object) -> str | None:
    if isinstance(v, bytes) and v:
        return ":".join(f"{b:02x}" for b in v)
    return None


async def enumerate_interfaces(host: str, creds: dict) -> list[dict]:
    async def col(base: str, n: int) -> dict:
        oid = f"{base}.{n}"
        return walk_to_index(await snmp_walk(host, creds, oid), oid)

    descr = await col(IF_TABLE, 2)
    if not descr:
        raise SnmpError("ifTable not available")
    types, speeds, macs = await col(IF_TABLE, 3), await col(IF_TABLE, 5), await col(IF_TABLE, 6)
    admin, oper = await col(IF_TABLE, 7), await col(IF_TABLE, 8)
    names: dict = {}
    high: dict = {}
    alias: dict = {}
    if creds.get("version") != "v1":
        try:
            names = await col(IF_XTABLE, 1)
            high = await col(IF_XTABLE, 15)
            alias = await col(IF_XTABLE, 18)
        except SnmpError:
            pass
    items = []
    for idx in sorted(descr, key=lambda x: int(x) if x.isdigit() else 0):
        if not idx.isdigit():
            continue
        hs = high.get(idx)
        speed = (
            int(hs) * 1_000_000
            if isinstance(hs, int) and hs > 0
            else (speeds.get(idx) if isinstance(speeds.get(idx), int) else None)
        )
        d_text = as_text(descr.get(idx))
        items.append(
            {
                "if_index": int(idx),
                "name": as_text(names.get(idx)) or d_text,
                "descr": d_text,
                "alias": as_text(alias.get(idx)),
                "type": types.get(idx) if isinstance(types.get(idx), int) else None,
                "speed_bps": speed,
                "admin_status": IF_STATUS.get(admin.get(idx), "unknown"),  # type: ignore[arg-type]
                "oper_status": IF_STATUS.get(oper.get(idx), "unknown"),  # type: ignore[arg-type]
                "mac": _mac(macs.get(idx)),
            }
        )
    return items


HR_STORAGE = "1.3.6.1.2.1.25.2.3.1"
STORAGE_TYPES = {
    "1.3.6.1.2.1.25.2.1.2": "ram",
    "1.3.6.1.2.1.25.2.1.3": "virtual_memory",
    "1.3.6.1.2.1.25.2.1.4": "fixed_disk",
    "1.3.6.1.2.1.25.2.1.5": "removable_disk",
    "1.3.6.1.2.1.25.2.1.9": "flash",
    "1.3.6.1.2.1.25.2.1.10": "network_disk",
}


def _u32(v: Any) -> int | None:
    if not isinstance(v, int):
        return None
    return v + 2**32 if v < 0 else v


async def enumerate_storage(host: str, creds: dict[str, Any]) -> list[dict[str, Any]]:
    """hrStorageTable rows: index, descr, type, size_bytes, used_bytes (sorted by index)."""

    async def col(n: int) -> dict[str, Any]:
        oid = f"{HR_STORAGE}.{n}"
        return walk_to_index(await snmp_walk(host, creds, oid, max_rows=500), oid)

    descr = await col(3)
    if not descr:
        raise SnmpError("hrStorageTable not available")
    types, units, size, used = await col(2), await col(4), await col(5), await col(6)
    items = []
    for idx in sorted((i for i in descr if i.isdigit()), key=int):
        u, sz, us = _u32(units.get(idx)), _u32(size.get(idx)), _u32(used.get(idx))
        items.append(
            {
                "index": int(idx),
                "descr": as_text(descr.get(idx)),
                "type": STORAGE_TYPES.get(str(types.get(idx) or "").lstrip("."), "other"),
                "size_bytes": sz * u if sz is not None and u is not None else None,
                "used_bytes": us * u if us is not None and u is not None else None,
            }
        )
    return items
