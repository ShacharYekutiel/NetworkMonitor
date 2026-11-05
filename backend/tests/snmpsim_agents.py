"""snmpsim helpers: one snmpsim-command-responder process per simulated device, bound to its
own loopback address (Linux routes all of 127.0.0.0/8 to lo)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from netmon.poller.snmp_client import SnmpError, snmp_get

Record = tuple[str, object, str]


def snmpsim_exe() -> str | None:
    exe = shutil.which("snmpsim-command-responder") or str(
        Path(sys.executable).parent / "snmpsim-command-responder"
    )
    return exe if Path(exe).exists() else None


def write_snmprec(path: Path, records: list[Record]) -> None:
    recs = sorted(records, key=lambda x: tuple(int(p) for p in x[0].split(".")))
    path.write_text("".join(f"{o}|{t}|{v}\n" for o, t, v in recs))


@dataclass
class Agent:
    ip: str
    port: int
    proc: subprocess.Popen[bytes]
    data: Path

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.data, ignore_errors=True)


async def _ready(ip: str, port: int, community: str) -> bool:
    creds = {"version": "v2c", "port": port, "timeout_s": 1, "retries": 0, "community": community}
    try:
        await snmp_get(ip, creds, ["1.3.6.1.2.1.1.5.0"])
        return True
    except SnmpError:
        return False


def start_agent(ip: str, port: int, files: dict[str, list[Record]]) -> Agent | None:
    """Start an agent at ip:port serving `files` ({community: records}). None on failure."""
    exe = snmpsim_exe()
    if exe is None:
        return None
    data = Path(tempfile.mkdtemp(prefix="netmon-snmpsim-"))
    data.chmod(0o755)
    for community, records in files.items():
        write_snmprec(data / f"{community}.snmprec", records)
    cache = data / "cache"
    cache.mkdir()
    args = [
        exe,
        f"--data-dir={data}",
        f"--cache-dir={cache}",
        f"--agent-udpv4-endpoint={ip}:{port}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(cache, "nobody")
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    community = next(iter(files))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if asyncio.run(_ready(ip, port, community)):
            return Agent(ip, port, proc, data)
        if proc.poll() is not None:
            break
    proc.kill()
    shutil.rmtree(data, ignore_errors=True)
    return None


# ------------------------------------------------------------------ simulated devices
SYS = "1.3.6.1.2.1.1"
IF = "1.3.6.1.2.1.2.2.1"
IFX = "1.3.6.1.2.1.31.1.1.1"
HR_STORAGE = "1.3.6.1.2.1.25.2.3.1"
HR_CPU = "1.3.6.1.2.1.25.3.3.1.2"


def system(descr: str, oid: str, name: str) -> list[Record]:
    return [
        (f"{SYS}.1.0", 4, descr),
        (f"{SYS}.2.0", 6, oid),
        (f"{SYS}.3.0", 67, "8640000"),
        (f"{SYS}.5.0", 4, name),
    ]


def interfaces(rows: list[tuple[int, str, int, int]]) -> list[Record]:
    """rows: (index, name, type, oper_status)."""
    out: list[Record] = []
    for idx, name, type_, oper in rows:
        out += [
            (f"{IF}.1.{idx}", 2, str(idx)),
            (f"{IF}.2.{idx}", 4, name),
            (f"{IF}.3.{idx}", 2, str(type_)),
            (f"{IF}.5.{idx}", 66, "1000000000"),
            (f"{IF}.7.{idx}", 2, "1"),
            (f"{IF}.8.{idx}", 2, str(oper)),
            (f"{IF}.10.{idx}", 65, "1000"),
            (f"{IF}.14.{idx}", 65, "0"),
            (f"{IF}.16.{idx}", 65, "2000"),
            (f"{IF}.20.{idx}", 65, "0"),
            (f"{IFX}.1.{idx}", 4, name),
            (f"{IFX}.6.{idx}", 70, "100000"),
            (f"{IFX}.10.{idx}", 70, "200000"),
            (f"{IFX}.15.{idx}", 66, "1000"),
        ]
    return out


def host_resources(storage: list[tuple[int, str, str, int, int, int]]) -> list[Record]:
    """storage: (index, type_oid, descr, units, size, used); plus two CPU rows."""
    out: list[Record] = [(f"{HR_CPU}.196608", 2, "10"), (f"{HR_CPU}.196609", 2, "30")]
    for idx, type_oid, descr, units, size, used in storage:
        out += [
            (f"{HR_STORAGE}.1.{idx}", 2, str(idx)),
            (f"{HR_STORAGE}.2.{idx}", 6, type_oid),
            (f"{HR_STORAGE}.3.{idx}", 4, descr),
            (f"{HR_STORAGE}.4.{idx}", 2, str(units)),
            (f"{HR_STORAGE}.5.{idx}", 2, str(size)),
            (f"{HR_STORAGE}.6.{idx}", 2, str(used)),
        ]
    return out


RAM = "1.3.6.1.2.1.25.2.1.2"
FIXED = "1.3.6.1.2.1.25.2.1.4"
VIRTUAL = "1.3.6.1.2.1.25.2.1.3"

OPNSENSE_DESCR = (
    "FreeBSD OPNsense.localdomain 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 "
    "stable/24.7-n267758-abc amd64"
)


def opnsense_records() -> list[Record]:
    return (
        system(OPNSENSE_DESCR, "1.3.6.1.4.1.8072.3.2.8", "OPNsense.localdomain")
        + interfaces(
            [
                (1, "igb0", 6, 1),
                (2, "igb1", 6, 1),
                (3, "lo0", 24, 1),
                (4, "pflog0", 1, 1),
                (5, "enc0", 1, 2),
            ]
        )
        + host_resources(
            [
                (1, RAM, "Physical memory", 4096, 2000000, 1000000),
                (31, FIXED, "/", 4096, 28000000, 1600000),
            ]
        )
    )


def unifi_records() -> list[Record]:
    vap = "1.3.6.1.4.1.41112.1.6.1.2.1"
    return (
        system(
            "Linux U6-Lite 4.4.153 #1 SMP Mon Aug 1 00:00:00 UTC 2022 mips",
            "1.3.6.1.4.1.8072.3.2.10",
            "U6-Lite-Office",
        )
        + interfaces([(1, "lo", 24, 1), (2, "eth0", 6, 1), (3, "br0", 209, 1), (4, "ath0", 71, 1)])
        + host_resources([(1, RAM, "Physical memory", 1024, 250000, 120000)])
        + [
            (f"{vap}.6.1", 4, "HomeWiFi"),
            (f"{vap}.6.2", 4, "HomeWiFi"),
            (f"{vap}.8.1", 66, "5"),
            (f"{vap}.8.2", 66, "12"),
            (f"{vap}.9.1", 4, "ng"),
            (f"{vap}.9.2", 4, "na"),
        ]
    )


def printer_records() -> list[Record]:
    sup = "1.3.6.1.2.1.43.11.1.1"
    return (
        system("Brother NC-8300h, Firmware Ver.1.10", "1.3.6.1.4.1.2435.2.3.9.1", "BRN3C2AF4")
        + interfaces([(1, "eth0", 6, 1)])
        + [
            ("1.3.6.1.2.1.43.10.2.1.4.1.1", 65, "12345"),
            (f"{sup}.6.1.1", 4, "Black Toner Cartridge"),
            (f"{sup}.8.1.1", 2, "100"),
            (f"{sup}.9.1.1", 2, "40"),
            (f"{sup}.6.1.2", 4, "Drum Unit"),
            (f"{sup}.8.1.2", 2, "100"),
            (f"{sup}.9.1.2", 2, "-3"),
        ]
    )


def cisco_records() -> list[Record]:
    return system(
        "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8",
        "1.3.6.1.4.1.9.1.1208",
        "core-sw",
    ) + interfaces(
        [(1, "Gi1/0/1", 6, 1), (2, "Gi1/0/2", 6, 1), (3, "Gi1/0/3", 6, 2), (4, "Gi1/0/4", 6, 2)]
    )
