// [tree-move] Moving devices and groups between groups (drag and drop in the device tree,
// multi-select, "Move to…"): selection, which drops are allowed and the request body. Pure helpers
// (unit-tested in treeMove.test.ts); the server checks the same rules (POST /api/tree/move).
import type { TreeDevice, TreeGroup } from '../api/types';

export type MoveKind = 'group' | 'device';
export type MoveRef = { kind: MoveKind; id: number };

/** "g12" / "d7": the key of a tree row (also the key of the tree's expanded map). */
export function refKey(r: MoveRef): string {
  return `${r.kind === 'group' ? 'g' : 'd'}${r.id}`;
}

export function parseKey(key: string): MoveRef | null {
  const m = /^([gd])(\d+)$/.exec(key);
  if (!m) return null;
  return { kind: m[1] === 'g' ? 'group' : 'device', id: Number(m[2]) };
}

export type MoveIndex = {
  groups: Map<number, TreeGroup>;
  devices: Map<number, { device: TreeDevice; groupId: number }>;
};

export function indexForMove(groups: TreeGroup[]): MoveIndex {
  const ix: MoveIndex = { groups: new Map(), devices: new Map() };
  const walk = (g: TreeGroup) => {
    ix.groups.set(g.id, g);
    for (const d of g.devices) ix.devices.set(d.id, { device: d, groupId: g.id });
    g.children.forEach(walk);
  };
  groups.forEach(walk);
  return ix;
}

/** [id, parent, grandparent, ...] (stops on an unknown id or a loop). */
export function ancestors(ix: MoveIndex, groupId: number | null): number[] {
  const out: number[] = [];
  const seen = new Set<number>();
  let cur = groupId;
  while (cur !== null && ix.groups.has(cur) && !seen.has(cur)) {
    out.push(cur);
    seen.add(cur);
    cur = ix.groups.get(cur)!.parent_id;
  }
  return out;
}

/** Where an item is now: the group of a device, the parent of a group (null = top level). */
export function locationOf(ix: MoveIndex, r: MoveRef): number | null | undefined {
  if (r.kind === 'device') return ix.devices.get(r.id)?.groupId;
  const g = ix.groups.get(r.id);
  return g ? g.parent_id : undefined;
}

/**
 * What actually moves: known items, each once; an item inside a group that moves too is dropped
 * (it goes along with that group).
 */
export function normalizeMove(ix: MoveIndex, refs: MoveRef[]): MoveRef[] {
  const seen = new Set<string>();
  const known = refs.filter((r) => {
    const k = refKey(r);
    if (seen.has(k) || locationOf(ix, r) === undefined) return false;
    seen.add(k);
    return true;
  });
  const moving = new Set(known.filter((r) => r.kind === 'group').map((r) => r.id));
  return known.filter((r) => {
    const chain =
      r.kind === 'group'
        ? ancestors(ix, r.id).slice(1)
        : ancestors(ix, ix.devices.get(r.id)!.groupId);
    return !chain.some((g) => moving.has(g));
  });
}

export type DropCheck =
  | 'ok'
  /** nothing to move */
  | 'empty'
  /** a group onto itself */
  | 'self'
  /** a group into one of its own descendants */
  | 'cycle'
  /** devices cannot be at the top level */
  | 'root'
  /** everything is already there */
  | 'same';

/** Can `refs` be dropped on `target` (a group id, or null = the top level)? */
export function dropCheck(ix: MoveIndex, refs: MoveRef[], target: number | null): DropCheck {
  const items = normalizeMove(ix, refs);
  if (!items.length) return 'empty';
  if (target !== null && !ix.groups.has(target)) return 'empty';
  if (target === null && items.some((r) => r.kind === 'device')) return 'root';
  const chain = ancestors(ix, target);
  for (const r of items) {
    if (r.kind !== 'group') continue;
    if (r.id === target) return 'self';
    if (chain.includes(r.id)) return 'cycle';
  }
  if (items.every((r) => locationOf(ix, r) === target)) return 'same';
  return 'ok';
}

export type MoveBody = {
  device_ids: number[];
  group_ids: number[];
  target_group_id: number | null;
};

/** The POST /api/tree/move body (items already in the target are left out). */
export function moveBody(ix: MoveIndex, refs: MoveRef[], target: number | null): MoveBody {
  const items = normalizeMove(ix, refs).filter((r) => locationOf(ix, r) !== target);
  return {
    device_ids: items.filter((r) => r.kind === 'device').map((r) => r.id),
    group_ids: items.filter((r) => r.kind === 'group').map((r) => r.id),
    target_group_id: target,
  };
}

export function moveCounts(refs: MoveRef[]): { devices: number; groups: number } {
  return {
    devices: refs.filter((r) => r.kind === 'device').length,
    groups: refs.filter((r) => r.kind === 'group').length,
  };
}

export function refName(ix: MoveIndex, r: MoveRef): string {
  return r.kind === 'group'
    ? (ix.groups.get(r.id)?.name ?? `#${r.id}`)
    : (ix.devices.get(r.id)?.device.name ?? `#${r.id}`);
}

// ---------------------------------------------------------------------------------------------
// Selection (Ctrl/Cmd-click toggles, Shift-click selects a range of the visible rows)

/**
 * The group and device rows in the order the tree shows them. `isOpen(key)` says whether a group
 * shows its content (collapsed groups hide theirs).
 */
export function visibleOrder(groups: TreeGroup[], isOpen: (key: string) => boolean): string[] {
  const out: string[] = [];
  const walk = (g: TreeGroup) => {
    const key = `g${g.id}`;
    out.push(key);
    if (!isOpen(key)) return;
    g.children.forEach(walk);
    for (const d of g.devices) out.push(`d${d.id}`);
  };
  groups.forEach(walk);
  return out;
}

export function toggleKey(sel: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(sel);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

/** The rows from `anchor` to `key` (inclusive, either direction); just `key` without an anchor. */
export function rangeKeys(order: string[], anchor: string | null, key: string): string[] {
  const b = order.indexOf(key);
  if (b < 0) return [];
  const a = anchor === null ? -1 : order.indexOf(anchor);
  if (a < 0) return [key];
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  return order.slice(lo, hi + 1);
}

export function selectedRefs(sel: Iterable<string>): MoveRef[] {
  return [...sel].map(parseKey).filter((r): r is MoveRef => r !== null);
}

/** The items a drag (or a "Move to…") of `ref` takes: the whole selection when it includes it. */
export function dragItems(sel: ReadonlySet<string>, ref: MoveRef): MoveRef[] {
  return sel.has(refKey(ref)) && sel.size > 1 ? selectedRefs(sel) : [ref];
}

// ---------------------------------------------------------------------------------------------
// The group picker of "Move to…"

export type PickerOption = {
  /** null = the top level */
  id: number | null;
  name: string;
  depth: number;
  check: DropCheck;
};

/** Every group (depth-first, as in the tree), plus the top level first when groups alone move. */
export function pickerOptions(
  groups: TreeGroup[],
  ix: MoveIndex,
  refs: MoveRef[],
  filter = '',
): PickerOption[] {
  const q = filter.trim().toLocaleLowerCase();
  const out: PickerOption[] = [];
  const walk = (g: TreeGroup, depth: number) => {
    if (!q || g.name.toLocaleLowerCase().includes(q))
      out.push({ id: g.id, name: g.name, depth: q ? 0 : depth, check: dropCheck(ix, refs, g.id) });
    g.children.forEach((c) => walk(c, depth + 1));
  };
  groups.forEach((g) => walk(g, 0));
  const items = normalizeMove(ix, refs);
  if (!q && items.length && items.every((r) => r.kind === 'group'))
    out.unshift({ id: null, name: '', depth: 0, check: dropCheck(ix, refs, null) });
  return out;
}

/** How long a collapsed group must be hovered during a drag before it opens. */
export const AUTO_EXPAND_MS = 700;
