"""[3b] Pure core of alert correlation (`correlate.cluster`, architecture § 14.8.2).

Two problems are linked when they are on the same device, the devices are dependency
parent/child (also transitive), topology neighbours over a non-stale link, or share a port key
(flap episodes / LAG). Problems are processed in time order; a problem joins every existing
group it is linked to whose last addition is within the window (the groups merge), else it
starts its own group. Groups with at least `min_alerts` problems are incidents."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PROBLEM_STATUSES = ("down", "unreachable", "warning")


@dataclass(frozen=True)
class Problem:
    event_id: int
    device_id: int | None
    sensor_id: int | None
    status: str
    time: datetime
    port_key: str | None = None


@dataclass
class Graph:
    """parents: device -> its dependency parent (effective, 2b-1); neighbours: non-stale
    topology links (undirected); names: device names for titles."""

    parents: dict[int, int] = field(default_factory=dict)
    neighbours: dict[int, set[int]] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)

    @staticmethod
    def build(
        parents: dict[int, int | None],
        links: Iterable[tuple[int, int]],
        names: dict[int, str] | None = None,
    ) -> Graph:
        g = Graph(names=dict(names or {}))
        g.parents = {c: p for c, p in parents.items() if p is not None and p != c}
        for a, b in links:
            if a is None or b is None or a == b:
                continue
            g.neighbours.setdefault(a, set()).add(b)
            g.neighbours.setdefault(b, set()).add(a)
        return g

    def ancestors(self, device_id: int) -> list[int]:
        out: list[int] = []
        seen = {device_id}
        cur = self.parents.get(device_id)
        while cur is not None and cur not in seen and len(out) < 64:
            out.append(cur)
            seen.add(cur)
            cur = self.parents.get(cur)
        return out

    def is_ancestor(self, a: int, b: int) -> bool:
        return a in self.ancestors(b)

    def neighbours_of(self, device_id: int) -> set[int]:
        return self.neighbours.get(device_id, set())


def linked(p: Problem, q: Problem, g: Graph) -> bool:
    if p.port_key and p.port_key == q.port_key:
        return True
    a, b = p.device_id, q.device_id
    if a is None or b is None:
        return False
    if a == b:
        return True
    if g.is_ancestor(a, b) or g.is_ancestor(b, a):
        return True
    return b in g.neighbours_of(a)


@dataclass
class Cluster:
    members: list[Problem]
    last_change: datetime

    @property
    def devices(self) -> set[int]:
        return {m.device_id for m in self.members if m.device_id is not None}


def cluster(
    problems: Iterable[Problem],
    graph: Graph,
    window_s: float = 300,
    min_alerts: int = 3,
    *,
    return_all: bool = False,
) -> list[Cluster]:
    """Group problem transitions (see the module docstring). Returns the clusters with at least
    `min_alerts` members (all clusters with `return_all`), oldest first."""
    groups: list[Cluster] = []
    for p in sorted(problems, key=lambda x: (x.time, x.event_id)):
        if p.status not in PROBLEM_STATUSES:
            continue
        hits = [
            c
            for c in groups
            if (p.time - c.last_change).total_seconds() <= window_s
            and any(linked(p, m, graph) for m in c.members)
        ]
        if not hits:
            groups.append(Cluster([p], p.time))
            continue
        target = hits[0]
        for other in hits[1:]:
            target.members.extend(other.members)
            groups.remove(other)
        target.members.append(p)
        target.members.sort(key=lambda x: (x.time, x.event_id))
        target.last_change = p.time
    if return_all:
        return groups
    return [c for c in groups if len(c.members) >= min_alerts]


def joins(
    p: Problem, members: list[Problem], last_change: datetime, graph: Graph, window_s: float
) -> bool:
    """Whether a new problem grows an existing incident."""
    if p.status not in PROBLEM_STATUSES:
        return False
    if (p.time - last_change).total_seconds() > window_s:
        return False
    return any(linked(p, m, graph) for m in members)


def root_candidates(members: list[Problem], graph: Graph) -> list[int]:
    """Ordered root candidates: `down` devices with `unreachable` dependents in the cluster (most
    dependents first); else devices by links to other members; else the earliest problem."""
    devs: dict[int, list[Problem]] = {}
    for m in members:
        if m.device_id is not None:
            devs.setdefault(m.device_id, []).append(m)
    if not devs:
        return []
    unreachable = {d for d, ps in devs.items() if any(p.status == "unreachable" for p in ps)}
    first_time = {d: min(p.time for p in ps) for d, ps in devs.items()}
    scored: list[tuple[int, datetime, int]] = []
    for d, ps in devs.items():
        if not any(p.status == "down" for p in ps):
            continue
        dependents = sum(1 for u in unreachable if u != d and graph.is_ancestor(d, u))
        if dependents:
            scored.append((-dependents, first_time[d], d))
    if scored:
        return [d for _, _, d in sorted(scored)]
    link_counts = []
    for d in devs:
        n = sum(
            1
            for o in devs
            if o != d
            and (graph.is_ancestor(d, o) or graph.is_ancestor(o, d) or o in graph.neighbours_of(d))
        )
        link_counts.append((-n, first_time[d], d))
    link_counts.sort()
    if link_counts and link_counts[0][0] < 0:
        return [d for n, _, d in link_counts if n < 0]
    earliest = min(members, key=lambda x: (x.time, x.event_id))
    return [earliest.device_id] if earliest.device_id is not None else []


def _root_status(members: list[Problem], root: int | None) -> str:
    order = {"down": 0, "unreachable": 1, "warning": 2}
    statuses = [m.status for m in members if m.device_id == root]
    if not statuses:
        return "down"
    return min(statuses, key=lambda s: order.get(s, 9))


def title_fields(members: list[Problem], root: int | None, graph: Graph) -> dict[str, Any]:
    """`{"root", "root_status", "count", "unit", "status"}` for the deterministic title."""
    root_status = _root_status(members, root)
    others = [m for m in members if m.device_id != root] or [m for m in members]
    devices = {m.device_id for m in members if m.device_id is not None}
    if len(devices) > 1:
        unit = "devices"
        by_status: dict[str, set[Any]] = {}
        for m in others:
            by_status.setdefault(m.status, set()).add(m.device_id)
        counts = Counter({s: len(v) for s, v in by_status.items()})
    else:
        unit = "sensors"
        counts = Counter(m.status for m in others)
    if counts:
        order = {"unreachable": 0, "down": 1, "warning": 2}
        status, count = max(counts.items(), key=lambda kv: (kv[1], -order.get(kv[0], 9)))
    else:
        status, count = root_status, 0
    root_name = graph.names.get(root, f"device {root}") if root is not None else "?"
    return {
        "root": root_name,
        "root_status": root_status,
        "count": int(count),
        "unit": unit,
        "status": status,
    }


def title_en(fields: dict[str, Any]) -> str:
    unit = fields["unit"]
    if fields["count"] == 1:
        unit = unit[:-1]
    return (
        f"{fields['root']} {fields['root_status']} — {fields['count']} {unit} {fields['status']}"
    )[:200]


HE_STATUS = {"down": "למטה", "unreachable": "לא נגיש", "warning": "אזהרה", "up": "תקין"}


def title_he(fields: dict[str, Any]) -> str:
    unit = "מכשירים" if fields["unit"] == "devices" else "חיישנים"
    st = HE_STATUS.get(fields["status"], fields["status"])
    rst = HE_STATUS.get(fields["root_status"], fields["root_status"])
    return f"{fields['root']} {rst} — {fields['count']} {unit} {st}"[:200]


def growth_due(member_count: int, summarized_count: int, summaries: int) -> bool:
    """A new AI summary on growth by ≥ 50 % (max 5 per incident; the 2-min spacing is checked
    by the caller)."""
    if summaries >= 5:
        return False
    if summaries == 0:
        return True
    return member_count >= summarized_count * 1.5
