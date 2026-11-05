"""SNMP diagnostic: compare the credentials a device uses for polling with the ones
that answered during discovery, and query the device with each.

Run inside the app container (prints no secrets; keys are shown only as length + short hash):

    PowerShell:
      Get-Content deploy\\tools\\snmp_diag.py |
        docker compose -f deploy/docker-compose.yml exec -T netmon python - [host ...]
    sh:
      docker compose -f deploy/docker-compose.yml exec -T netmon python - [host ...] \\
        < deploy/tools/snmp_diag.py

Without hosts it checks every device that has an SNMP sensor.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys

from sqlalchemy import select, text

SECRET = ("community", "v3_auth_key", "v3_priv_key")
SHOW = (
    "version",
    "port",
    "timeout_s",
    "retries",
    "v3_username",
    "v3_security_level",
    "v3_auth_protocol",
    "v3_priv_protocol",
)
SYS_NAME = "1.3.6.1.2.1.1.5.0"


def fp(value: object) -> str:
    if not value:
        return "-"
    s = str(value)
    return f"len={len(s)} sha={hashlib.sha256(s.encode()).hexdigest()[:8]}"


def describe(c: dict | None) -> str:
    if not c:
        return "(none)"
    parts = [f"{k}={c.get(k)}" for k in SHOW if c.get(k) not in (None, "")]
    parts += [f"{k}[{fp(c.get(k))}]" for k in SECRET if c.get(k)]
    return " ".join(parts)


async def probe(host: str, creds: dict) -> str:
    from netmon.poller.snmp_client import SnmpError, snmp_get

    try:
        r = await snmp_get(host, dict(creds), [SYS_NAME])
        v = r.get(SYS_NAME)
        return f"OK sysName={v.decode(errors='replace') if isinstance(v, bytes) else v}"
    except SnmpError as exc:
        return f"FAIL {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {exc}"


async def main(hosts: list[str]) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from netmon.config import load_settings
    from netmon.db.engine import make_engine
    from netmon.models.discovery import DiscoveryResult, DiscoveryScan
    from netmon.models.inventory import Device, Sensor
    from netmon.poller.snmp_client import resolve_credentials
    from netmon.services.sensors import group_chain

    engine = make_engine(load_settings().async_database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        q = select(Device).order_by(Device.id)
        if hosts:
            q = q.where(Device.host.in_(hosts))
        else:
            snmp_dev = select(Sensor.device_id).where(Sensor.type.like("snmp_%"))
            q = q.where(Device.id.in_(snmp_dev))
        devices = list((await s.execute(q)).scalars())
        if not devices:
            print("No matching devices.")
        for d in devices:
            chain = await group_chain(s, d.group_id)
            creds, source, gid = resolve_credentials(d.snmp_credentials, chain)
            print(f"\n=== device #{d.id} {d.name} host={d.host} enabled={d.enabled}")
            print(f"  polling creds (source={source}{'' if gid is None else f', group #{gid}'}):")
            print(f"    {describe(creds)}")
            print(f"    query: {await probe(d.host, creds)}")
            slow = {**creds, "timeout_s": 6, "retries": 2}
            print(f"    query (timeout 6s, 2 retries): {await probe(d.host, slow)}")

            row = (
                await s.execute(
                    select(DiscoveryResult, DiscoveryScan)
                    .join(DiscoveryScan, DiscoveryScan.id == DiscoveryResult.scan_id)
                    .where(text("host(discovery_results.ip) = :h"))
                    .params(h=d.host)
                    .where(DiscoveryResult.snmp_ok.is_(True))
                    .order_by(DiscoveryScan.id.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                print("  discovery: no scan result with SNMP for this host")
                continue
            res, scan = row
            idx = res.snmp_credential_index
            tried = scan.credentials or []
            used = tried[idx] if idx is not None and 0 <= idx < len(tried) else None
            print(f"  discovery scan #{scan.id}: answered with credential #{idx}")
            print(f"    {describe(used)}")
            if used:
                same = all(used.get(k) == creds.get(k) for k in (*SHOW[:2], *SHOW[4:], *SECRET))
                print(f"    same as polling creds (ignoring timeout/retries): {same}")
                print(f"    query with scan creds: {await probe(d.host, used)}")
    await engine.dispose()


asyncio.run(main(sys.argv[1:]))
