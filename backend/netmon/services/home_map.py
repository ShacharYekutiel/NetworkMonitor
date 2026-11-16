"""[premium] Home network map (`GET /api/overview/map`, data-model "[premium] Home map").

A small tree for the home screen, computed from the inventory status rollup (the same one
`/api/tree` uses), the open problem sensors and the topology links. No SNMP; a handful of
queries, safe to poll every ~10 s. Never guesses a layout: the backbone chain is the one the
user chose in the `home_map` setting; without it the tree is the scope's child groups under a
virtual root (and `configured: false` when the topology suggests a backbone exists)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.services.status import Inventory, severity_key

KEY = "home_map"
DEFAULT: dict[str, Any] = {
    "mode": "auto",
    "show_internet": True,
    "backbone_device_ids": [],
    "max_groups": 12,
}
MODES = ("auto", "backbone")
MAX_BACKBONE = 8
MAX_GROUPS = (1, 50)
PROBLEM_MAX = 80
PROBLEM_STATUSES = ("down", "warning", "unreachable")

_FIREWALL = re.compile(
    r"firewall|fortigate|fortinet|palo ?alto|pan-os|opnsense|pfsense|check ?point|sophos|"
    r"watchguard|sonicwall|\bsrx\d*\b|\basa\s?\d{4}|\bfw\b",
    re.I,
)

TEXT = {
    "en": {"devices": "{n} devices", "device": "1 device", "more": "+{n}", "internet": "Internet"},
    "he": {"devices": "{n} התקנים", "device": "התקן אחד", "more": "+{n}", "internet": "אינטרנט"},
}


def normalize(value: Any) -> dict[str, Any]:
    """The stored setting merged over the defaults (unknown keys dropped)."""
    out = dict(DEFAULT)
    if isinstance(value, dict):
        for k in DEFAULT:
            if k in value and value[k] is not None:
                out[k] = value[k]
    out["backbone_device_ids"] = [int(x) for x in out["backbone_device_ids"] or []]
    return out


def looks_like_firewall(name: str, identity: dict[str, Any] | None) -> bool:
    parts = [name]
    for k in ("vendor", "model", "suggested_template", "sys_descr"):
        v = (identity or {}).get(k)
        if isinstance(v, str):
            parts.append(v)
    return bool(_FIREWALL.search(" ".join(parts)))


def link_status(child_status: str) -> str:
    if child_status in ("down", "unreachable"):
        return "down"
    if child_status == "warning":
        return "warning"
    if child_status == "unknown":
        return "unknown"
    return "up"


def _cut(s: str, n: int = PROBLEM_MAX) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


class Builder:
    """Pure: everything is passed in (tests build it without a database)."""

    def __init__(
        self,
        inv: Inventory,
        problems: list[dict[str, Any]],
        identities: dict[int, dict[str, Any] | None],
        links: list[tuple[int, int]],
        language: str = "en",
    ) -> None:
        self.inv = inv
        self.identities = identities
        self.links = links
        self.t = TEXT.get(language, TEXT["en"])
        # problems: sensor dicts (id, device_id, name, status, message, acked, since) sorted
        # worst first: severity, unacknowledged first, oldest change first, id
        self.problems = sorted(
            problems,
            key=lambda p: (
                -severity_key(p["status"]),
                bool(p.get("acked")),
                p.get("since") is not None,
                p.get("since") or 0,
                p["id"],
            ),
        )

    # ---------------------------------------------------------------- scope helpers
    def scope_devices(self, gid: int | None) -> set[int]:
        if gid is None:
            return set(self.inv.devices)
        groups = set(self.inv.descendants(gid))
        return {d for d, info in self.inv.devices.items() if info["group_id"] in groups}

    def child_groups(self, gid: int | None) -> list[int]:
        return sorted(
            self.inv.children.get(gid, []),
            key=lambda c: (self.inv.groups[c]["sort_order"], self.inv.groups[c]["name"].lower()),
        )

    def counts(self, devices: set[int]) -> dict[str, int]:
        out = {"devices": len(devices), "sensors": 0, "down": 0, "warning": 0, "unreachable": 0}
        for d in devices:
            out["sensors"] += self.inv.device_counts[d]["total"]
            st = self.inv.device_status[d]
            if st in ("down", "warning", "unreachable"):
                out[st] += 1
        return out

    def problem(self, devices: set[int], with_device: bool) -> str | None:
        for p in self.problems:
            if p["device_id"] in devices:
                if with_device:
                    return _cut(f"{self.inv.devices[p['device_id']]['name']} · {p['name']}")
                msg = (p.get("message") or "").strip()
                return _cut(f"{p['name']}: {msg}" if msg else p["name"])
        return None

    def subtitle(self, n: int) -> str:
        return self.t["device"] if n == 1 else self.t["devices"].format(n=n)

    # ---------------------------------------------------------------- nodes
    def group_node(self, gid: int, parent: str | None) -> dict[str, Any]:
        devs = self.scope_devices(gid)
        return {
            "id": f"g{gid}",
            "kind": "group",
            "role": "group",
            "name": self.inv.groups[gid]["name"],
            "subtitle": self.subtitle(len(devs)),
            "status": self.inv.group_status[gid],
            "problem": self.problem(devs, True),
            "counts": self.counts(devs),
            "ref": {"type": "group", "id": gid},
            "parent_id": parent,
        }

    def device_node(self, did: int, parent: str | None, role: str | None) -> dict[str, Any]:
        info = self.inv.devices[did]
        ident = self.identities.get(did) or {}
        sub = ident.get("model") or ident.get("vendor") or info["host"]
        return {
            "id": f"d{did}",
            "kind": "device",
            "role": role,
            "name": info["name"],
            "subtitle": str(sub or ""),
            "status": self.inv.device_status[did],
            "problem": self.problem({did}, False),
            "counts": self.counts({did}),
            "ref": {"type": "device", "id": did},
            "parent_id": parent,
        }

    def leaves(self, gid: int | None, parent: str, exclude: set[int], max_n: int) -> list[dict]:
        """Child groups of the scope, then the scope's own devices (not on the backbone); at
        most `max_n`, problem nodes kept first when cut, the rest summed in a `+N` node."""
        nodes = [self.group_node(c, parent) for c in self.child_groups(gid)]
        if gid is not None:
            own = sorted(
                (d for d in self.inv.group_devices.get(gid, []) if d not in exclude),
                key=lambda d: self.inv.devices[d]["name"].lower(),
            )
            nodes += [self.device_node(d, parent, None) for d in own]
        if len(nodes) <= max_n:
            return nodes
        keep = max(max_n - 1, 1)
        ranked = sorted(
            range(len(nodes)), key=lambda i: (nodes[i]["status"] not in PROBLEM_STATUSES, i)
        )
        kept = sorted(ranked[:keep])
        rest = [nodes[i] for i in range(len(nodes)) if i not in set(kept)]
        devs: set[int] = set()
        for n in rest:
            ref = n["ref"]
            devs |= self.scope_devices(ref["id"]) if ref["type"] == "group" else {ref["id"]}
        worst = max((n["status"] for n in rest), key=severity_key)
        more = {
            "id": "more",
            "kind": "group",
            "role": "group",
            "name": self.t["more"].format(n=len(rest)),
            "subtitle": self.subtitle(len(devs)),
            "status": worst,
            "problem": self.problem(devs, True),
            "counts": self.counts(devs),
            "ref": None,
            "parent_id": parent,
        }
        return [nodes[i] for i in kept] + [more]

    def scope_status(self, gid: int | None) -> str:
        if gid is not None:
            return self.inv.group_status[gid]
        from netmon.services.status import group_status

        tops = [g for g in self.inv.children.get(None, []) if self.inv.group_has_members[g]]
        return group_status([self.inv.group_status[g] for g in tops])

    def hub_suggested(self, gid: int | None) -> bool:
        """True when one device has topology links into most (>= 2 and >= half) of the scope's
        child groups: a backbone exists that the user has not chosen yet."""
        kids = self.child_groups(gid)
        if len(kids) < 2:
            return False
        group_of: dict[int, int] = {}
        for k in kids:
            for d in self.scope_devices(k):
                group_of[d] = k
        reach: dict[int, set[int]] = {}
        for a, b in self.links:
            for x, y in ((a, b), (b, a)):
                if y in group_of:
                    reach.setdefault(x, set()).add(group_of[y])
        need = max(2, (len(kids) + 1) // 2)
        for x, groups in reach.items():
            if x in group_of:
                groups = groups | {group_of[x]}
            if len(groups) >= need:
                return True
        return False

    def build(self, gid: int | None, scope_name: str, setting: dict[str, Any]) -> dict[str, Any]:
        cfg = normalize(setting)
        max_n = max(MAX_GROUPS[0], min(MAX_GROUPS[1], int(cfg["max_groups"])))
        in_scope = self.scope_devices(gid)
        backbone = [
            d for d in cfg["backbone_device_ids"] if d in self.inv.devices and d in in_scope
        ]
        nodes: list[dict[str, Any]] = []
        configured = True
        if cfg["mode"] == "backbone" and backbone:
            parent: str | None = None
            if cfg["show_internet"]:
                nodes.append(
                    {
                        "id": "internet",
                        "kind": "internet",
                        "role": "internet",
                        "name": self.t["internet"],
                        "subtitle": "",
                        "status": "unknown",
                        "problem": None,
                        "counts": self.counts(set()),
                        "ref": None,
                        "parent_id": None,
                    }
                )
                parent = "internet"
            for i, did in enumerate(backbone):
                role = "core"
                if i == 0 and looks_like_firewall(
                    self.inv.devices[did]["name"], self.identities.get(did)
                ):
                    role = "firewall"
                node = self.device_node(did, parent, role)
                nodes.append(node)
                parent = node["id"]
            assert parent is not None
            nodes += self.leaves(gid, parent, set(backbone), max_n)
        else:
            devs = in_scope
            nodes.append(
                {
                    "id": "root",
                    "kind": "group",
                    "role": None,
                    "name": scope_name,
                    "subtitle": self.subtitle(len(devs)),
                    "status": self.scope_status(gid),
                    "problem": self.problem(devs, True),
                    "counts": self.counts(devs),
                    "ref": {"type": "group", "id": gid} if gid is not None else None,
                    "parent_id": None,
                }
            )
            nodes += self.leaves(gid, "root", set(), max_n)
            configured = not self.hub_suggested(gid)
        by_id = {n["id"]: n for n in nodes}
        links = [
            {"from": n["parent_id"], "to": n["id"], "status": link_status(n["status"])}
            for n in nodes
            if n["parent_id"] is not None and n["parent_id"] in by_id
        ]
        return {
            "scope": {"group_id": gid, "name": scope_name},
            "nodes": nodes,
            "links": links,
            "configured": configured,
        }


async def load_problems(session: AsyncSession, inv: Inventory) -> list[dict[str, Any]]:
    ids = [sid for sid, s in inv.sensors.items() if s["status"] in PROBLEM_STATUSES]
    if not ids:
        return []
    out = []
    for r in await session.execute(
        text(
            "SELECT id, device_id, name, status, status_message, ack_at IS NOT NULL AS acked, "
            "last_status_change_at FROM sensors WHERE id = ANY(:i)"
        ),
        {"i": ids},
    ):
        out.append(
            {
                "id": r.id,
                "device_id": r.device_id,
                "name": r.name,
                "status": r.status,
                "message": r.status_message or "",
                "acked": bool(r.acked),
                "since": r.last_status_change_at.timestamp() if r.last_status_change_at else None,
            }
        )
    return out


async def load_identities(session: AsyncSession, ids: list[int]) -> dict[int, Any]:
    if not ids:
        return {}
    return {
        r.id: r.identity
        for r in await session.execute(
            text("SELECT id, identity FROM devices WHERE id = ANY(:i)"), {"i": sorted(ids)}
        )
    }


async def load_links(session: AsyncSession) -> list[tuple[int, int]]:
    return [
        (r.a_device_id, r.b_device_id)
        for r in await session.execute(
            text(
                "SELECT a_device_id, b_device_id FROM topology_links "
                "WHERE NOT stale AND b_device_id IS NOT NULL"
            )
        )
    ]
