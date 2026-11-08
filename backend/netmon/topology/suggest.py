"""[2b-1] Dependency suggestions from the topology graph (pure BFS)."""

from __future__ import annotations

from collections.abc import Iterable


def degree(edges: Iterable[tuple[int, int]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for a, b in edges:
        if a == b:
            continue
        out[a] = out.get(a, 0) + 1
        out[b] = out.get(b, 0) + 1
    return out


def pick_root(edges: list[tuple[int, int]]) -> int | None:
    """The device with the most links (ties -> lowest id)."""
    deg = degree(edges)
    if not deg:
        return None
    return min(deg, key=lambda d: (-deg[d], d))


def suggest_dependencies(
    edges: list[tuple[int, int]], root_device_id: int | None = None
) -> tuple[int | None, dict[int, tuple[int | None, int]]]:
    """edges: non-stale device-device links (manual included). Returns (root, {device_id:
    (suggested parent | None, hops)}) for every device reached from the root (root: parent
    None, hops 0). Parent = BFS predecessor; ties -> the neighbour with more links, then the
    lowest id."""
    deg = degree(edges)
    adj: dict[int, set[int]] = {}
    for a, b in edges:
        if a == b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    root = root_device_id if root_device_id is not None else pick_root(edges)
    if root is None:
        return None, {}
    result: dict[int, tuple[int | None, int]] = {root: (None, 0)}
    frontier = [root]
    hops = 0
    while frontier:
        hops += 1
        candidates: dict[int, list[int]] = {}
        for node in frontier:
            for nb in adj.get(node, ()):
                if nb not in result:
                    candidates.setdefault(nb, []).append(node)
        for nb, preds in candidates.items():
            parent = min(preds, key=lambda p: (-deg.get(p, 0), p))
            result[nb] = (parent, hops)
        frontier = sorted(candidates)
    return root, result
