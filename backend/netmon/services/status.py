"""Status rollup (computed on read, see data-model.md "Status rollup")."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.poller.types import SEVERITY, worst

STATUSES = ("up", "warning", "down", "paused", "unknown", "unreachable")


def empty_counts() -> dict[str, int]:
    return {
        "up": 0,
        "warning": 0,
        "down": 0,
        "paused": 0,
        "unknown": 0,
        "unreachable": 0,
        "total": 0,
    }


def add_counts(a: dict[str, int], b: dict[str, int]) -> None:
    for k in a:
        a[k] += b[k]


def device_status(enabled: bool, sensor_statuses: list[str]) -> str:
    if not enabled:
        return "paused"
    if not sensor_statuses:
        return "unknown"
    active = [s for s in sensor_statuses if s != "paused"]
    if not active:
        return "paused"
    return worst(active)


def group_status(member_statuses: list[str]) -> str:
    """member_statuses: statuses of direct devices and non-empty child groups."""
    if not member_statuses:
        return "unknown"
    active = [s for s in member_statuses if s != "paused"]
    if not active:
        return "paused"
    return worst(active)


@dataclass
class Inventory:
    groups: dict[int, dict[str, Any]] = field(default_factory=dict)
    devices: dict[int, dict[str, Any]] = field(default_factory=dict)
    sensors: dict[int, dict[str, Any]] = field(default_factory=dict)
    children: dict[int | None, list[int]] = field(default_factory=dict)
    group_devices: dict[int, list[int]] = field(default_factory=dict)
    device_sensors: dict[int, list[int]] = field(default_factory=dict)
    device_status: dict[int, str] = field(default_factory=dict)
    device_counts: dict[int, dict[str, int]] = field(default_factory=dict)
    group_status: dict[int, str] = field(default_factory=dict)
    group_counts: dict[int, dict[str, int]] = field(default_factory=dict)
    group_has_members: dict[int, bool] = field(default_factory=dict)

    def ancestors(self, group_id: int) -> list[int]:
        """[group_id, parent, grandparent, ...]"""
        out: list[int] = []
        cur: int | None = group_id
        seen: set[int] = set()
        while cur is not None and cur in self.groups and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = self.groups[cur]["parent_id"]
        return out

    def descendants(self, group_id: int) -> list[int]:
        out = [group_id]
        i = 0
        while i < len(out):
            out.extend(self.children.get(out[i], []))
            i += 1
        return out

    def effective_snmp(self, device_id: int) -> tuple[str, int | None]:
        d = self.devices[device_id]
        if d["snmp_credentials"] is not None:
            return "device", None
        for gid in self.ancestors(d["group_id"]):
            if self.groups[gid]["snmp_credentials"] is not None:
                return "group", gid
        return "default", None

    def compute(self) -> None:
        for gid in self.groups:
            self.children.setdefault(gid, [])
            self.group_devices.setdefault(gid, [])
        for gid, g in self.groups.items():
            self.children.setdefault(g["parent_id"], []).append(gid)
        for did, d in self.devices.items():
            self.group_devices.setdefault(d["group_id"], []).append(did)
            self.device_sensors.setdefault(did, [])
        for sid, s in self.sensors.items():
            self.device_sensors.setdefault(s["device_id"], []).append(sid)
        for did, d in self.devices.items():
            statuses = [self.sensors[s]["status"] for s in self.device_sensors[did]]
            c = empty_counts()
            for st in statuses:
                c[st] += 1
                c["total"] += 1
            self.device_counts[did] = c
            self.device_status[did] = device_status(d["enabled"], statuses)

        def visit(gid: int, stack: set[int]) -> None:
            if gid in self.group_status:
                return
            stack.add(gid)
            counts = empty_counts()
            members: list[str] = []
            has_members = False
            for did in self.group_devices.get(gid, []):
                add_counts(counts, self.device_counts[did])
                members.append(self.device_status[did])
                has_members = True
            for cid in self.children.get(gid, []):
                if cid in stack:
                    continue
                visit(cid, stack)
                add_counts(counts, self.group_counts[cid])
                if self.group_has_members[cid]:
                    members.append(self.group_status[cid])
                    has_members = True
            self.group_counts[gid] = counts
            self.group_status[gid] = group_status(members)
            self.group_has_members[gid] = has_members
            stack.discard(gid)

        for gid in self.groups:
            visit(gid, set())

    def snapshot(self) -> dict[tuple[str, int], tuple[str, tuple[int, ...]]]:
        snap: dict[tuple[str, int], tuple[str, tuple[int, ...]]] = {}
        for did in self.devices:
            snap[("device", did)] = (
                self.device_status[did],
                tuple(self.device_counts[did].values()),
            )
        for gid in self.groups:
            snap[("group", gid)] = (self.group_status[gid], tuple(self.group_counts[gid].values()))
        return snap


async def load_inventory(session: AsyncSession) -> Inventory:
    inv = Inventory()
    for row in await session.execute(
        text(
            "SELECT id, parent_id, name, snmp_credentials, sort_order, dependency_mode, "
            "dependency_device_id, dependency_sensor_id FROM groups"
        )
    ):
        inv.groups[row.id] = {
            "parent_id": row.parent_id,
            "name": row.name,
            "snmp_credentials": row.snmp_credentials,
            "sort_order": row.sort_order,
            "dependency_mode": row.dependency_mode,
            "dependency_device_id": row.dependency_device_id,
            "dependency_sensor_id": row.dependency_sensor_id,
        }
    for row in await session.execute(
        text(
            "SELECT id, group_id, name, host, enabled, snmp_credentials, dependency_mode, "
            "dependency_device_id, dependency_sensor_id FROM devices"
        )
    ):
        inv.devices[row.id] = {
            "group_id": row.group_id,
            "name": row.name,
            "host": row.host,
            "enabled": row.enabled,
            "snmp_credentials": row.snmp_credentials,
            "dependency_mode": row.dependency_mode,
            "dependency_device_id": row.dependency_device_id,
            "dependency_sensor_id": row.dependency_sensor_id,
        }
    for row in await session.execute(
        text(
            "SELECT id, device_id, name, type, enabled, sort_order, status, "
            "ack_at IS NOT NULL AS acked FROM sensors"
        )
    ):
        inv.sensors[row.id] = {
            "device_id": row.device_id,
            "status": row.status,
            "name": row.name,
            "type": row.type,
            "enabled": row.enabled,
            "sort_order": row.sort_order,
            "acknowledged": row.acked,
        }
    inv.compute()
    return inv


def severity_key(status: str) -> int:
    return SEVERITY.get(status, 0)
