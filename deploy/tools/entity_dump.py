"""Print a device's hardware tree (ENTITY-MIB) plus the HPE Comware health columns, using the
SNMP credentials the device polls with. Read-only; prints no secrets. Paste the output back
when a template names sensors wrongly.

Run inside the app container, with the device's IP (as configured in NetworkMonitor):

    PowerShell:
      Get-Content deploy\\tools\\entity_dump.py |
        docker compose -f deploy/docker-compose.yml exec -T netmon python - 10.0.0.2
    sh:
      docker compose -f deploy/docker-compose.yml exec -T netmon python - 10.0.0.2 \\
        < deploy/tools/entity_dump.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

ENT = "1.3.6.1.2.1.47.1.1.1.1"
HH3C = "1.3.6.1.4.1.25506.2.6.1.1.1.1"
CLASSES = {
    1: "other",
    2: "unknown",
    3: "chassis",
    4: "backplane",
    5: "container",
    6: "powerSupply",
    7: "fan",
    8: "sensor",
    9: "module",
    10: "port",
    11: "stack",
    12: "cpu",
}
# column -> header; ENTITY-MIB descr/containedIn/class/relPos/name/model, Comware cpu/mem/temp/err
COLUMNS = {
    f"{ENT}.2": "descr",
    f"{ENT}.4": "parent",
    f"{ENT}.5": "class",
    f"{ENT}.6": "pos",
    f"{ENT}.7": "name",
    f"{ENT}.13": "model",
    f"{HH3C}.6": "cpu%",
    f"{HH3C}.8": "mem%",
    f"{HH3C}.12": "temp",
    f"{HH3C}.19": "err",
}


def text(v: object) -> str:
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    return "" if v is None else str(v)


async def main(host: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from netmon.config import load_settings
    from netmon.db.engine import make_engine
    from netmon.models.inventory import Device
    from netmon.poller.snmp_client import SnmpError, resolve_credentials, snmp_walk
    from netmon.services.sensors import group_chain

    engine = make_engine(load_settings().async_database_url)
    async with async_sessionmaker(engine)() as s:
        d = (await s.execute(select(Device).where(Device.host == host))).scalars().first()
        if d is None:
            print(f"No device with host {host}.")
            return
        creds, _, _ = resolve_credentials(d.snmp_credentials, await group_chain(s, d.group_id))
    await engine.dispose()
    creds = {**creds, "timeout_s": 6, "retries": 2}
    table: dict[str, dict[str, str]] = {}
    for oid, col in COLUMNS.items():
        try:
            rows = await snmp_walk(host, creds, oid, max_rows=5000)
        except SnmpError as exc:
            print(f"# {col}: {exc}")
            continue
        for r_oid, value in rows:
            idx = str(r_oid)[len(oid) + 1 :]
            table.setdefault(idx, {})[col] = text(value)
    print(f"# {d.name} ({host}): {len(table)} entities")
    print("index | parent | class | pos | name | model | cpu% | mem% | temp | err | descr")
    for idx in sorted(table, key=lambda x: [int(p) for p in x.split(".") if p.isdigit()]):
        r = table[idx]
        cls = r.get("class", "")
        cls = CLASSES.get(int(cls), cls) if cls.isdigit() else cls
        cells = [idx, r.get("parent", ""), cls, r.get("pos", ""), r.get("name", "")]
        cells += [r.get(k, "") for k in ("model", "cpu%", "mem%", "temp", "err")]
        cells.append(r.get("descr", "")[:60])
        print(" | ".join(cells))


if len(sys.argv) != 2:
    print("usage: python - <device IP>")
else:
    asyncio.run(main(sys.argv[1]))
