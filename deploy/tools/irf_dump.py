"""Print the IRF stack tables of an HPE Comware / H3C switch (HH3C-STACK-MIB, hh3cStack
1.3.6.1.4.1.25506.2.91) using the SNMP credentials the device polls with. Read-only (GET /
walk only); prints no secrets. Send the output back so the IRF template entries
(data-model "[1.3] IRF sensors") can be verified on the real stack.

Run inside the app container, with the device's IP (as configured in NetworkMonitor):

    PowerShell:
      Get-Content deploy\\tools\\irf_dump.py |
        docker compose -f deploy/docker-compose.yml exec -T netmon python - 10.0.0.2
    sh:
      docker compose -f deploy/docker-compose.yml exec -T netmon python - 10.0.0.2 \\
        < deploy/tools/irf_dump.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

STACK = "1.3.6.1.4.1.25506.2.91"
ENT_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
# hh3cStackGlobalConfig scalars
SCALARS = {
    f"{STACK}.1.1.0": "hh3cStackMaxMember",
    f"{STACK}.1.2.0": "hh3cStackMemberNum",
    f"{STACK}.1.3.0": "hh3cStackMaxConfigPriority",
    f"{STACK}.1.7.0": "hh3cStackTopology",
    f"{STACK}.1.8.0": "hh3cStackDomainId",
}
# (title, {column OID: header}) per table
TABLES = [
    (
        "hh3cStackDeviceConfigTable (INDEX entPhysicalIndex)",
        {
            f"{STACK}.2.1.1": "memberID",
            f"{STACK}.2.1.2": "configMemberID",
            f"{STACK}.2.1.3": "priority",
            f"{STACK}.2.1.4": "portNum",
            f"{STACK}.2.1.5": "portMaxNum",
        },
    ),
    (
        "hh3cStackBoardConfigTable (INDEX entPhysicalIndex; role slave(1) master(2) loading(3) "
        "other(4))",
        {f"{STACK}.3.1.1": "role", f"{STACK}.3.1.2": "belongtoMember"},
    ),
    (
        "hh3cStackPortInfoTable (INDEX memberID.portIndex; status up(1) down(2) silent(3) "
        "disabled(4))",
        {
            f"{STACK}.4.1.2": "enable",
            f"{STACK}.4.1.3": "status",
            f"{STACK}.4.1.4": "neighbor",
            f"{STACK}.4.1.5": "forwardingPath",
        },
    ),
    (
        "hh3cStackPhyPortInfoTable (INDEX entPhysicalIndex)",
        {f"{STACK}.5.1.1": "belongtoPort"},
    ),
]


def text(v: object) -> str:
    if isinstance(v, bytes):
        try:
            s = v.decode()
            return s if s.isprintable() else v.hex(":")
        except UnicodeDecodeError:
            return v.hex(":")
    return "" if v is None else str(v)


def _key(idx: str) -> list[int]:
    return [int(p) for p in idx.split(".") if p.isdigit()]


async def main(host: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from netmon.config import load_settings
    from netmon.db.engine import make_engine
    from netmon.models.inventory import Device
    from netmon.poller.snmp_client import SnmpError, resolve_credentials, snmp_get, snmp_walk
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
    print(f"# {d.name} ({host})")
    try:
        got = await snmp_get(host, creds, list(SCALARS))
        for oid, name in SCALARS.items():
            print(f"{name} ({oid}) = {text(got.get(oid))}")
    except SnmpError as exc:
        print(f"# scalars: {exc}")
    names: dict[str, str] = {}
    try:
        for r_oid, value in await snmp_walk(host, creds, ENT_NAME, max_rows=5000):
            names[str(r_oid)[len(ENT_NAME) + 1 :]] = text(value)
    except SnmpError as exc:
        print(f"# entPhysicalName: {exc}")
    for title, cols in TABLES:
        rows: dict[str, dict[str, str]] = {}
        for oid, col in cols.items():
            try:
                walked = await snmp_walk(host, creds, oid, max_rows=5000)
            except SnmpError as exc:
                print(f"# {col}: {exc}")
                continue
            for r_oid, value in walked:
                rows.setdefault(str(r_oid)[len(oid) + 1 :], {})[col] = text(value)
        print()
        print(f"## {title}: {len(rows)} rows")
        entity = "entPhysicalIndex" in title.split(";")[0]
        header = ["index", *cols.values()] + (["entPhysicalName"] if entity else [])
        print(" | ".join(header))
        for idx in sorted(rows, key=_key):
            cells = [idx, *(rows[idx].get(c, "") for c in cols.values())]
            if entity:
                cells.append(names.get(idx, ""))
            print(" | ".join(cells))


if len(sys.argv) != 2:
    print("usage: python - <device IP>")
else:
    asyncio.run(main(sys.argv[1]))
