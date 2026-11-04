import type { Status, StatusCounts, Tree, TreeDevice, TreeGroup, TreeSensor } from '../api/types';

function mapGroups(groups: TreeGroup[], fn: (g: TreeGroup) => TreeGroup): TreeGroup[] {
  return groups.map((g) => {
    const children = mapGroups(g.children, fn);
    return fn(children === g.children ? g : { ...g, children });
  });
}

export function patchTreeSensor(tree: Tree, id: number, patch: Partial<TreeSensor>): Tree {
  let changed = false;
  const groups = mapGroups(tree.groups, (g) => {
    let devChanged = false;
    const devices = g.devices.map((d) => {
      if (!d.sensors.some((s) => s.id === id)) return d;
      devChanged = true;
      changed = true;
      return { ...d, sensors: d.sensors.map((s) => (s.id === id ? { ...s, ...patch } : s)) };
    });
    return devChanged ? { ...g, devices } : g;
  });
  return changed ? { groups } : tree;
}

export function patchTreeDevice(
  tree: Tree,
  id: number,
  patch: { status: Status; status_counts: StatusCounts },
): Tree {
  const groups = mapGroups(tree.groups, (g) =>
    g.devices.some((d) => d.id === id)
      ? { ...g, devices: g.devices.map((d) => (d.id === id ? { ...d, ...patch } : d)) }
      : g,
  );
  return { groups };
}

export function patchTreeGroup(
  tree: Tree,
  id: number,
  patch: { status: Status; status_counts: StatusCounts },
): Tree {
  return { groups: mapGroups(tree.groups, (g) => (g.id === id ? { ...g, ...patch } : g)) };
}

export function flattenGroups(
  groups: TreeGroup[],
  depth = 0,
): { group: TreeGroup; depth: number }[] {
  const out: { group: TreeGroup; depth: number }[] = [];
  for (const g of groups) {
    out.push({ group: g, depth });
    out.push(...flattenGroups(g.children, depth + 1));
  }
  return out;
}

export function findGroup(groups: TreeGroup[], id: number): TreeGroup | null {
  for (const g of groups) {
    if (g.id === id) return g;
    const c = findGroup(g.children, id);
    if (c) return c;
  }
  return null;
}

/** The chain of groups from the root to the group with `id` (inclusive). */
export function groupPath(groups: TreeGroup[], id: number): TreeGroup[] {
  for (const g of groups) {
    if (g.id === id) return [g];
    const sub = groupPath(g.children, id);
    if (sub.length) return [g, ...sub];
  }
  return [];
}

export function findDevice(
  groups: TreeGroup[],
  id: number,
): { device: TreeDevice; group: TreeGroup } | null {
  for (const g of groups) {
    const d = g.devices.find((x) => x.id === id);
    if (d) return { device: d, group: g };
    const c = findDevice(g.children, id);
    if (c) return c;
  }
  return null;
}

export function findSensor(
  groups: TreeGroup[],
  id: number,
): { sensor: TreeSensor; device: TreeDevice; group: TreeGroup } | null {
  for (const g of groups) {
    for (const d of g.devices) {
      const s = d.sensors.find((x) => x.id === id);
      if (s) return { sensor: s, device: d, group: g };
    }
    const c = findSensor(g.children, id);
    if (c) return c;
  }
  return null;
}

/** Ids of groups whose subtree contains `id` (inclusive) — used to exclude cycle targets. */
export function descendantGroupIds(group: TreeGroup): number[] {
  return [group.id, ...group.children.flatMap(descendantGroupIds)];
}
