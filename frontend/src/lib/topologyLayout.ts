// Topology map: the default arrangement (pure, deterministic, overlap-free).
//
// Coordinates are the graph series' data coordinates. The chart shows them at 1 data unit = 1 CSS px
// at zoom 1 (TopologyChart sizes the series box to the bounding box), and nodes scale with the
// zoom, so a layout without overlaps in data units never overlaps on screen.
//
// - Linked components: tiers. The most central device (smallest sum of hop distances, then device
//   class, link count and name) is on top, its neighbours below, breadth-first; leaves of one parent
//   wrap into a compact block under it. Several components sit side by side.
// - Unlinked devices: a grid per group (in the order of the group tree), below the linked part
//   (or beside it, when that fits the canvas aspect better).
// - Saved positions are kept; nodes without one (or sharing one with another node) are placed in
//   free space next to their neighbours or their group's grid.
import type { DeviceClass } from '../api/types';

export type LayoutNode = {
  key: string;
  name: string;
  kind: 'device' | 'neighbor';
  /** the device's group; null for an unmanaged neighbour */
  groupId: number | null;
  deviceClass?: DeviceClass | null;
  /** stored (or locally moved) position, null when there is none */
  pos: [number, number] | null;
};
export type LayoutLink = { a: string; b: string };
export type GroupInfo = { name: string; order: number };
export type ArrangeStyle = 'tiered' | 'groups';
export type Pos = Record<string, [number, number]>;

export type LayoutOptions = {
  /** label pill width (px) of a node name; default: an estimate for Inter 12/500 */
  measure?: (name: string) => number;
  groups?: ReadonlyMap<number, GroupInfo>;
  /** wanted width / height of the whole picture (the canvas aspect) */
  aspect?: number;
};

// --- geometry (px at zoom 1; must match TopologyChart: 72px symbol, name pill under the tile) ---
/** symbol box of a node (the visible tile is 56px) */
export const TILE = 72;
/** footprint of a node around its centre: tile top … bottom of the name pill */
export const FOOT_TOP = 32;
export const FOOT_BOTTOM = 56;
const GAP_X = 28;
const GAP_Y = 30;
/** vertical distance between two tiers (room for the links) */
export const TIER_STEP = FOOT_TOP + FOOT_BOTTOM + 72;
/** vertical distance between two grid rows */
export const ROW_STEP = FOOT_TOP + FOOT_BOTTOM + GAP_Y;
/** room above a group's grid for its caption */
export const CAPTION_H = 34;
const BLOCK_GAP_X = 96;
const BLOCK_GAP_Y = 84;
/** saved positions closer than this are treated as one spot (a pile) */
const SAME_SPOT = 24;

const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });
const byName = (a: { name: string; key: string }, b: { name: string; key: string }) =>
  collator.compare(a.name, b.name) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);

/** Estimated pill width of a name: Inter 12px/500 averages ~6.9px a character, plus padding and dot. */
export function estimateLabelWidth(name: string): number {
  return Math.ceil(name.length * 6.9) + 34;
}

function halfWidth(n: LayoutNode, measure: (s: string) => number): number {
  return Math.max(TILE / 2, measure(n.name) / 2);
}

/** Footprint rectangle [x1, y1, x2, y2] of a node at (x, y). */
export function footprint(x: number, y: number, half: number): [number, number, number, number] {
  return [x - half, y - FOOT_TOP, x + half, y + FOOT_BOTTOM];
}

function overlaps(a: number[], b: number[], margin = 0): boolean {
  return (
    a[0] < b[2] + margin && b[0] < a[2] + margin && a[1] < b[3] + margin && b[1] < a[3] + margin
  );
}

const FIREWALL = /(^|[^a-z])(fw|firewall|fortigate|asa|pfsense|palo)([^a-z]|$)/i;
function classRank(n: LayoutNode): number {
  if (n.kind === 'neighbor') return -1;
  if (FIREWALL.test(n.name)) return 3;
  if (n.deviceClass === 'router') return 3;
  if (n.deviceClass === 'switch') return 2;
  return 0;
}

type Placed = { key: string; x: number; y: number };
type Block = { w: number; h: number; items: Placed[]; label?: string };

// ---------------------------------------------------------------------------------------------
// Tiered layout of one linked component

type Ctx = {
  byKey: Map<string, LayoutNode>;
  adj: Map<string, string[]>;
  half: (key: string) => number;
};

function bfsDist(adj: Map<string, string[]>, from: string): Map<string, number> {
  const d = new Map([[from, 0]]);
  const q = [from];
  for (let i = 0; i < q.length; i++) {
    const v = q[i];
    for (const w of adj.get(v) ?? [])
      if (!d.has(w)) {
        d.set(w, d.get(v)! + 1);
        q.push(w);
      }
  }
  return d;
}

/** Top tier of a component: the most central node, plus adjacent nodes as central as it. */
function topTier(keys: string[], ctx: Ctx): string[] {
  const score = new Map<string, number>();
  for (const k of keys) {
    let s = 0;
    for (const v of bfsDist(ctx.adj, k).values()) s += v;
    score.set(k, s);
  }
  const ranked = [...keys].sort((a, b) => {
    const na = ctx.byKey.get(a)!;
    const nb = ctx.byKey.get(b)!;
    return (
      score.get(a)! - score.get(b)! ||
      classRank(nb) - classRank(na) ||
      (ctx.adj.get(b)?.length ?? 0) - (ctx.adj.get(a)?.length ?? 0) ||
      byName(na, nb)
    );
  });
  const root = ranked[0];
  const best = score.get(root)!;
  const top = [root];
  for (const w of ctx.adj.get(root) ?? [])
    if (score.get(w)! <= best * 1.05 && classRank(ctx.byKey.get(w)!) >= 0) top.push(w);
  return top.sort((a, b) => byName(ctx.byKey.get(a)!, ctx.byKey.get(b)!));
}

/** Breadth-first spanning tree from the top tier; a node's parent is the least loaded candidate. */
function spanningTree(top: string[], ctx: Ctx): Map<string, string[]> {
  const children = new Map<string, string[]>();
  const order = new Map<string, number>();
  top.forEach((k, i) => order.set(k, i));
  let tier = top;
  const seen = new Set(top);
  while (tier.length) {
    const next = new Map<string, string[]>();
    for (const p of tier)
      for (const w of ctx.adj.get(p) ?? [])
        if (!seen.has(w)) {
          if (!next.has(w)) next.set(w, []);
          next.get(w)!.push(p);
        }
    const nodes = [...next.keys()].sort((a, b) => {
      const pa = Math.min(...next.get(a)!.map((p) => order.get(p)!));
      const pb = Math.min(...next.get(b)!.map((p) => order.get(p)!));
      return pa - pb || byName(ctx.byKey.get(a)!, ctx.byKey.get(b)!);
    });
    for (const w of nodes) {
      const parent = next
        .get(w)!
        .sort(
          (a, b) =>
            (children.get(a)?.length ?? 0) - (children.get(b)?.length ?? 0) ||
            order.get(a)! - order.get(b)!,
        )[0];
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent)!.push(w);
      seen.add(w);
    }
    // order of the next tier: by parent order, then name (keeps siblings together)
    const nextTier: string[] = [];
    for (const p of tier) {
      const c = (children.get(p) ?? []).sort((a, b) =>
        byName(ctx.byKey.get(a)!, ctx.byKey.get(b)!),
      );
      nextTier.push(...c);
    }
    nextTier.forEach((k, i) => order.set(k, i));
    tier = nextTier;
  }
  return children;
}

/** Subtree of `v`: its width and node positions relative to (left edge, v's row). */
function layoutSubtree(
  v: string,
  children: Map<string, string[]>,
  ctx: Ctx,
  leafCols: number,
): { w: number; items: Placed[] } {
  const own = 2 * ctx.half(v) + GAP_X;
  const kids = children.get(v) ?? [];
  if (!kids.length) return { w: own, items: [{ key: v, x: own / 2, y: 0 }] };
  const leaves = kids.filter((k) => !(children.get(k)?.length ?? 0));
  const inner = kids.filter((k) => children.get(k)?.length ?? 0);
  const parts: { w: number; items: Placed[] }[] = inner.map((k) =>
    layoutSubtree(k, children, ctx, leafCols),
  );
  if (leaves.length) {
    const cols = Math.min(leaves.length, leafCols);
    const cell = Math.max(...leaves.map((k) => 2 * ctx.half(k) + GAP_X));
    const items: Placed[] = [];
    leaves.forEach((k, i) => {
      const r = Math.floor(i / cols);
      const inRow = Math.min(cols, leaves.length - r * cols);
      const pad = ((cols - inRow) * cell) / 2; // the last row is centred
      items.push({ key: k, x: pad + (i % cols) * cell + cell / 2, y: r * ROW_STEP });
    });
    // the leaf block goes in the middle of the inner subtrees
    parts.splice(Math.ceil(parts.length / 2), 0, { w: cols * cell, items });
  }
  const kidsW = parts.reduce((s, p) => s + p.w, 0);
  const w = Math.max(own, kidsW);
  let x = (w - kidsW) / 2;
  const items: Placed[] = [{ key: v, x: w / 2, y: 0 }];
  for (const p of parts) {
    for (const it of p.items) items.push({ key: it.key, x: it.x + x, y: it.y + TIER_STEP });
    x += p.w;
  }
  return { w, items };
}

function bounds(items: Placed[], ctx: Ctx): [number, number, number, number] {
  let x1 = Infinity,
    y1 = Infinity,
    x2 = -Infinity,
    y2 = -Infinity;
  for (const it of items) {
    const h = ctx.half(it.key);
    x1 = Math.min(x1, it.x - h);
    x2 = Math.max(x2, it.x + h);
    y1 = Math.min(y1, it.y - FOOT_TOP);
    y2 = Math.max(y2, it.y + FOOT_BOTTOM);
  }
  return [x1, y1, x2, y2];
}

/** A block with its items moved so that its footprint starts at (0, 0). */
function normalise(items: Placed[], ctx: Ctx, label?: string): Block {
  const [x1, y1, x2, y2] = bounds(items, ctx);
  return {
    w: x2 - x1,
    h: y2 - y1,
    items: items.map((it) => ({ key: it.key, x: it.x - x1, y: it.y - y1 })),
    label,
  };
}

const LEAF_COLS = [2, 3, 4, 5, 6, 8, 10, 12, 16, 24, Infinity];

function tieredBlock(keys: string[], ctx: Ctx, aspect: number): Block {
  const top = topTier(keys, ctx);
  const children = spanningTree(top, ctx);
  let best: Block | null = null;
  let bestScore = Infinity;
  for (const cols of LEAF_COLS) {
    const items: Placed[] = [];
    let x = 0;
    for (const r of top) {
      const s = layoutSubtree(r, children, ctx, cols);
      for (const it of s.items) items.push({ key: it.key, x: it.x + x, y: it.y });
      x += s.w;
    }
    const b = normalise(items, ctx);
    const score = Math.abs(Math.log(b.w / b.h / aspect));
    if (score < bestScore - 1e-9) {
      best = b;
      bestScore = score;
    }
  }
  return best!;
}

/** Grid of unlinked nodes (one group), name order, about `aspect` wide. */
function gridBlock(keys: string[], ctx: Ctx, aspect: number, label?: string): Block {
  const nodes = keys.map((k) => ctx.byKey.get(k)!).sort(byName);
  const cell = Math.max(...nodes.map((n) => 2 * ctx.half(n.key) + GAP_X));
  const cols = Math.max(
    1,
    Math.min(nodes.length, Math.round(Math.sqrt((aspect * nodes.length * ROW_STEP) / cell)) || 1),
  );
  const top = label ? CAPTION_H : 0;
  const items = nodes.map((n, i) => ({
    key: n.key,
    x: (i % cols) * cell + cell / 2,
    y: top + FOOT_TOP + Math.floor(i / cols) * ROW_STEP,
  }));
  const rows = Math.ceil(nodes.length / cols);
  return {
    w: cols * cell - GAP_X,
    h: top + FOOT_TOP + (rows - 1) * ROW_STEP + FOOT_BOTTOM,
    items: items.map((it) => ({ ...it, x: it.x - GAP_X / 2 })),
    label,
  };
}

/** Shelf packing: blocks left to right, wrapping at `maxW`; returns placed items and the size. */
function pack(blocks: Block[], maxW: number): Block {
  const items: Placed[] = [];
  let x = 0,
    y = 0,
    rowH = 0,
    w = 0;
  for (const b of blocks) {
    if (x > 0 && x + b.w > maxW) {
      y += rowH + BLOCK_GAP_Y;
      x = 0;
      rowH = 0;
    }
    for (const it of b.items) items.push({ key: it.key, x: it.x + x, y: it.y + y });
    w = Math.max(w, x + b.w);
    rowH = Math.max(rowH, b.h);
    x += b.w + BLOCK_GAP_X;
  }
  return { w, h: y + rowH, items };
}

function components(keys: string[], ctx: Ctx): string[][] {
  const seen = new Set<string>();
  const out: string[][] = [];
  for (const k of keys) {
    if (seen.has(k)) continue;
    const comp = [...bfsDist(ctx.adj, k).keys()].filter((x) => !seen.has(x));
    comp.forEach((x) => seen.add(x));
    out.push(comp);
  }
  return out;
}

function makeCtx(
  nodes: LayoutNode[],
  links: LayoutLink[],
  opts: LayoutOptions,
  keys?: Set<string>,
) {
  const measure = opts.measure ?? estimateLabelWidth;
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const adjSet = new Map<string, Set<string>>();
  for (const n of nodes) if (!keys || keys.has(n.key)) adjSet.set(n.key, new Set());
  for (const l of links) {
    if (l.a === l.b || !adjSet.has(l.a) || !adjSet.has(l.b)) continue;
    adjSet.get(l.a)!.add(l.b);
    adjSet.get(l.b)!.add(l.a);
  }
  const adj = new Map<string, string[]>();
  for (const [k, s] of adjSet)
    adj.set(
      k,
      [...s].sort((a, b) => byName(byKey.get(a)!, byKey.get(b)!)),
    );
  const halves = new Map<string, number>();
  const half = (k: string) => {
    let h = halves.get(k);
    if (h === undefined) {
      h = halfWidth(byKey.get(k)!, measure);
      halves.set(k, h);
    }
    return h;
  };
  return { byKey, adj, half } satisfies Ctx;
}

function groupKey(n: LayoutNode): number {
  return n.groupId ?? -1;
}

function groupOrder(groups: ReadonlyMap<number, GroupInfo> | undefined) {
  return (a: number, b: number) => {
    const oa = groups?.get(a)?.order ?? Number.MAX_SAFE_INTEGER;
    const ob = groups?.get(b)?.order ?? Number.MAX_SAFE_INTEGER;
    return oa - ob || a - b;
  };
}

/** Linked components as tiers side by side, unlinked nodes in group grids below them. */
function tieredLayout(keys: string[], ctx: Ctx, opts: LayoutOptions, withCaptions: boolean): Block {
  const aspect = opts.aspect ?? 1.6;
  const comps = components(
    [...keys].sort((a, b) => byName(ctx.byKey.get(a)!, ctx.byKey.get(b)!)),
    ctx,
  );
  const linked = comps.filter((c) => c.length > 1);
  const alone = comps.filter((c) => c.length === 1).map((c) => c[0]);
  const compBlocks = linked
    .map((c) => tieredBlock(c, ctx, aspect))
    .sort((a, b) => b.items.length - a.items.length || 0);
  const byGroup = new Map<number, string[]>();
  for (const k of alone) {
    const g = groupKey(ctx.byKey.get(k)!);
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g)!.push(k);
  }
  const groupIds = [...byGroup.keys()].sort(groupOrder(opts.groups));
  const grids = (gridAspect: number) =>
    groupIds.map((g) =>
      gridBlock(
        byGroup.get(g)!,
        ctx,
        gridAspect,
        withCaptions ? (opts.groups?.get(g)?.name ?? undefined) : undefined,
      ),
    );
  const off = (b: Block) => Math.abs(Math.log(b.w / Math.max(b.h, 1) / aspect));
  let best: Block | null = null;
  // the grids' own shape is chosen too (squarer or wider), so they fill the width of the picture
  for (const gridAspect of [aspect, 1, 1.6, 2.4, 3.6, 5.4, 8]) {
    const gridBlocks = grids(gridAspect);
    // the linked part on top, the grids below (both centred on the wider one); the wrap width that
    // brings the whole picture closest to the wanted aspect wins
    const compose = (width: number): Block => {
      const parts: Block[] = [];
      if (compBlocks.length) parts.push(pack(compBlocks, width));
      if (gridBlocks.length) parts.push(pack(gridBlocks, Math.max(width, parts[0]?.w ?? 0)));
      const w = Math.max(...parts.map((p) => p.w));
      const items: Placed[] = [];
      let y = 0;
      for (const p of parts) {
        const dx = (w - p.w) / 2;
        for (const it of p.items) items.push({ key: it.key, x: it.x + dx, y: it.y + y });
        y += p.h + BLOCK_GAP_Y;
      }
      return { w, h: y - BLOCK_GAP_Y, items };
    };
    const candidates = [bestPacking([...compBlocks, ...gridBlocks], aspect, compose)];
    if (compBlocks.length && gridBlocks.length) {
      // or the grids flow on beside the linked part
      const all = [...compBlocks, ...gridBlocks];
      candidates.push(bestPacking(all, aspect, (w) => pack(all, w)));
    }
    for (const c of candidates) if (!best || off(c) < off(best) - 0.02) best = c;
  }
  return best!;
}

/** Tries wrap widths from the widest block to everything in one row; the closest aspect wins. */
function bestPacking(blocks: Block[], aspect: number, compose: (width: number) => Block): Block {
  const lo = Math.max(...blocks.map((b) => b.w));
  const hi = Math.max(
    lo,
    blocks.reduce((s, b) => s + b.w + BLOCK_GAP_X, 0),
  );
  let best: Block | null = null;
  let bestScore = Infinity;
  for (let i = 0; i <= 24; i++) {
    const b = compose(lo + ((hi - lo) * i) / 24);
    const score = Math.abs(Math.log(b.w / Math.max(b.h, 1) / aspect));
    if (score < bestScore - 1e-9) {
      best = b;
      bestScore = score;
    }
  }
  return best!;
}

/** Each group in its own block (its links as tiers inside), blocks in group-tree order. */
function groupsLayout(nodes: LayoutNode[], links: LayoutLink[], opts: LayoutOptions): Block {
  const aspect = opts.aspect ?? 1.6;
  const all = makeCtx(nodes, links, opts);
  // unmanaged neighbours join the group of their first linked device
  const home = new Map<string, number>();
  for (const n of [...nodes].sort(byName)) {
    if (n.kind === 'device') home.set(n.key, groupKey(n));
  }
  for (const n of [...nodes].sort(byName))
    if (n.kind === 'neighbor') {
      const dev = (all.adj.get(n.key) ?? []).find((k) => all.byKey.get(k)?.kind === 'device');
      home.set(n.key, dev !== undefined ? home.get(dev)! : -1);
    }
  const byGroup = new Map<number, string[]>();
  for (const n of nodes) {
    const g = home.get(n.key)!;
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g)!.push(n.key);
  }
  const blocks = [...byGroup.keys()].sort(groupOrder(opts.groups)).map((g) => {
    const keys = byGroup.get(g)!;
    const ctx = makeCtx(nodes, links, opts, new Set(keys));
    const inner = tieredLayout(keys, ctx, { ...opts, aspect }, false);
    const label = opts.groups?.get(g)?.name;
    const shift = label ? CAPTION_H : 0;
    return normaliseWithCaption(inner, all, label, shift);
  });
  return bestPacking(blocks, aspect, (w) => pack(blocks, w));
}

function normaliseWithCaption(b: Block, ctx: Ctx, label: string | undefined, shift: number): Block {
  const n = normalise(b.items, ctx);
  return {
    w: n.w,
    h: n.h + shift,
    items: n.items.map((it) => ({ ...it, y: it.y + shift })),
    label,
  };
}

function centred(items: Placed[], ctx: Ctx): Pos {
  const [x1, y1, x2, y2] = bounds(items, ctx);
  const cx = (x1 + x2) / 2;
  const cy = (y1 + y2) / 2;
  const out: Pos = {};
  for (const it of items) out[it.key] = [round(it.x - cx), round(it.y - cy)];
  return out;
}

const round = (v: number) => Math.round(v * 10) / 10;

/**
 * The default arrangement of all `nodes` (saved positions are ignored): `tiered` (linked parts as
 * tiers, unlinked devices in group grids) or `groups` (one block per group). Centred on (0, 0).
 */
export function arrangeAll(
  nodes: LayoutNode[],
  links: LayoutLink[],
  style: ArrangeStyle = 'tiered',
  opts: LayoutOptions = {},
): Pos {
  if (!nodes.length) return {};
  const ctx = makeCtx(nodes, links, opts);
  const block =
    style === 'groups'
      ? groupsLayout(nodes, links, opts)
      : tieredLayout(
          nodes.map((n) => n.key),
          ctx,
          opts,
          true,
        );
  return centred(block.items, ctx);
}

/**
 * Saved positions that are usable: finite, and not on the same spot as another node's (a pile from
 * an old layout: the first node by name keeps the spot, the others are placed again).
 */
export function usablePositions(nodes: LayoutNode[]): Pos {
  const out: Pos = {};
  const kept: [number, number][] = [];
  for (const n of [...nodes].sort(byName)) {
    const p = n.pos;
    if (!p || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) continue;
    if (kept.some((q) => Math.hypot(q[0] - p[0], q[1] - p[1]) < SAME_SPOT)) continue;
    kept.push(p);
    out[n.key] = [p[0], p[1]];
  }
  return out;
}

/**
 * Positions for the nodes that have none (or whose saved spot is taken): the result holds only
 * those nodes. No usable saved position at all → the default arrangement. Otherwise each new node
 * goes to the nearest free spot under its placed neighbours, or next to its group's unlinked devices,
 * or below the picture; when most nodes are new they are arranged as a block beside the saved ones.
 */
export function completeLayout(
  nodes: LayoutNode[],
  links: LayoutLink[],
  opts: LayoutOptions = {},
): Pos {
  const keep = usablePositions(nodes);
  const missing = nodes.filter((n) => !keep[n.key]);
  if (!missing.length) return {};
  if (!Object.keys(keep).length) return arrangeAll(nodes, links, 'tiered', opts);
  const ctx = makeCtx(nodes, links, opts);
  const rects: number[][] = Object.entries(keep).map(([k, [x, y]]) => footprint(x, y, ctx.half(k)));
  const placed: Pos = { ...keep };
  const out: Pos = {};
  if (missing.length > Object.keys(keep).length) {
    // many new nodes: their own default arrangement, beside the saved part
    const sub = arrangeAll(missing, links, 'tiered', opts);
    const subItems = Object.entries(sub).map(([key, [x, y]]) => ({ key, x, y }));
    const [sx1, sy1, sx2, sy2] = bounds(subItems, ctx);
    const keptItems = Object.entries(keep).map(([key, [x, y]]) => ({ key, x, y }));
    const [kx1, ky1, kx2, ky2] = bounds(keptItems, ctx);
    const aspect = opts.aspect ?? 1.6;
    const kw = kx2 - kx1,
      kh = ky2 - ky1,
      sw = sx2 - sx1,
      sh = sy2 - sy1;
    const off = (w: number, h: number) => Math.abs(Math.log(w / h / aspect));
    const toRight =
      off(kw + BLOCK_GAP_X + sw, Math.max(kh, sh)) < off(Math.max(kw, sw), kh + BLOCK_GAP_Y + sh);
    const dx = toRight ? kx2 + BLOCK_GAP_X - sx1 : kx1 - sx1;
    const dy = toRight ? ky1 - sy1 : ky2 + BLOCK_GAP_Y - sy1;
    for (const it of subItems) out[it.key] = [round(it.x + dx), round(it.y + dy)];
    return out;
  }
  const adj = ctx.adj;
  // linked new nodes first, breadth-first from the placed ones; then the unlinked ones
  const queue = missing
    .filter((n) => (adj.get(n.key) ?? []).some((k) => placed[k]))
    .sort(byName)
    .map((n) => n.key);
  const queued = new Set(queue);
  const order: string[] = [];
  for (let i = 0; i < queue.length; i++) {
    order.push(queue[i]);
    for (const w of adj.get(queue[i]) ?? [])
      if (!placed[w] && !queued.has(w)) {
        queued.add(w);
        queue.push(w);
      }
  }
  const rest = missing.filter((n) => !queued.has(n.key)).sort(byName);
  for (const n of rest) {
    // a new component of only new nodes: placed through its first node like the others
    if (queued.has(n.key)) continue;
    queued.add(n.key);
    order.push(n.key);
    for (const k of bfsDist(adj, n.key).keys())
      if (!queued.has(k) && !placed[k]) {
        queued.add(k);
        order.push(k);
      }
  }
  const lonely = (k: string) => !(adj.get(k)?.length ?? 0);
  for (const key of order) {
    const n = ctx.byKey.get(key)!;
    const half = ctx.half(key);
    const nbrs = (adj.get(key) ?? []).filter((k) => placed[k]);
    let pref: [number, number];
    if (nbrs.length) {
      const mx = nbrs.reduce((s, k) => s + placed[k][0], 0) / nbrs.length;
      const my = Math.max(...nbrs.map((k) => placed[k][1]));
      pref = [mx, my + TIER_STEP];
    } else {
      const mates = Object.keys(placed).filter(
        (k) => lonely(k) && groupKey(ctx.byKey.get(k)!) === groupKey(n),
      );
      if (mates.length && lonely(key)) {
        const maxY = Math.max(...mates.map((k) => placed[k][1]));
        const last = mates
          .filter((k) => placed[k][1] > maxY - ROW_STEP / 2)
          .reduce((a, b) => (placed[a][0] >= placed[b][0] ? a : b));
        pref = [placed[last][0] + ctx.half(last) + GAP_X + half, placed[last][1]];
      } else {
        const all = Object.entries(placed).map(([k, [x, y]]) => ({ key: k, x, y }));
        const [x1, , , y2] = bounds(all, ctx);
        pref = [x1 + half, y2 + BLOCK_GAP_Y + FOOT_TOP];
      }
    }
    const p = freeSpot(pref, half, rects);
    placed[key] = p;
    out[key] = [round(p[0]), round(p[1])];
    rects.push(footprint(p[0], p[1], half));
  }
  return out;
}

/** The free spot nearest to `pref` on a lattice around it (rings of growing radius). */
function freeSpot(pref: [number, number], half: number, rects: number[][]): [number, number] {
  const sx = 2 * half + GAP_X;
  const sy = ROW_STEP;
  const fits = (x: number, y: number) => {
    const f = footprint(x, y, half);
    return !rects.some((r) => overlaps(f, r, GAP_X / 2 - 1));
  };
  for (let r = 0; r < 400; r++) {
    const ring: [number, number, number][] = [];
    for (let i = -r; i <= r; i++)
      for (let j = -r; j <= r; j++) {
        if (Math.max(Math.abs(i), Math.abs(j)) !== r) continue;
        const x = pref[0] + (i * sx) / 2;
        const y = pref[1] + j * sy;
        ring.push([Math.hypot(x - pref[0], y - pref[1]), x, y]);
      }
    ring.sort((a, b) => a[0] - b[0] || a[2] - b[2] || a[1] - b[1]);
    for (const [, x, y] of ring) if (fits(x, y)) return [x, y];
  }
  return pref;
}

/**
 * Group captions over the unlinked devices of each group, while those devices still sit together
 * (no other node inside their box): the caption is centred above the box.
 */
export function groupCaptions(
  nodes: LayoutNode[],
  links: LayoutLink[],
  positions: Pos,
  opts: LayoutOptions = {},
): { groupId: number; name: string; x: number; y: number }[] {
  if (!opts.groups) return [];
  const ctx = makeCtx(nodes, links, opts);
  const byGroup = new Map<number, string[]>();
  for (const n of nodes)
    if (
      n.kind === 'device' &&
      n.groupId !== null &&
      !(ctx.adj.get(n.key)?.length ?? 0) &&
      positions[n.key]
    ) {
      if (!byGroup.has(n.groupId)) byGroup.set(n.groupId, []);
      byGroup.get(n.groupId)!.push(n.key);
    }
  const out: { groupId: number; name: string; x: number; y: number }[] = [];
  for (const [g, keys] of [...byGroup].sort((a, b) => groupOrder(opts.groups)(a[0], b[0]))) {
    const info = opts.groups.get(g);
    if (!info) continue;
    const items = keys.map((k) => ({ key: k, x: positions[k][0], y: positions[k][1] }));
    const [x1, y1, x2, y2] = bounds(items, ctx);
    const own = new Set(keys);
    const intruder = nodes.some((n) => {
      const p = positions[n.key];
      if (!p || own.has(n.key)) return false;
      return overlaps(footprint(p[0], p[1], ctx.half(n.key)), [x1, y1 - CAPTION_H, x2, y2]);
    });
    if (intruder) continue;
    out.push({
      groupId: g,
      name: info.name,
      x: round((x1 + x2) / 2),
      y: round(y1 - CAPTION_H / 2),
    });
  }
  return out;
}

/** Overlapping node pairs (footprints intersect) — for tests and the test hook. */
export function overlappingPairs(
  nodes: LayoutNode[],
  positions: Pos,
  opts: LayoutOptions = {},
): [string, string][] {
  const measure = opts.measure ?? estimateLabelWidth;
  const list = nodes
    .filter((n) => positions[n.key])
    .map((n) => ({ key: n.key, r: footprint(...positions[n.key], halfWidth(n, measure)) }));
  const out: [string, string][] = [];
  for (let i = 0; i < list.length; i++)
    for (let j = i + 1; j < list.length; j++)
      if (overlaps(list[i].r, list[j].r)) out.push([list[i].key, list[j].key]);
  return out;
}

/** Bounding box of positions as [minX, minY, maxX, maxY] of the centres (null when empty). */
export function centreBounds(
  positions: Pos,
  keys: string[],
): [number, number, number, number] | null {
  let x1 = Infinity,
    y1 = Infinity,
    x2 = -Infinity,
    y2 = -Infinity;
  for (const k of keys) {
    const p = positions[k];
    if (!p) continue;
    x1 = Math.min(x1, p[0]);
    y1 = Math.min(y1, p[1]);
    x2 = Math.max(x2, p[0]);
    y2 = Math.max(y2, p[1]);
  }
  return Number.isFinite(x1) ? [x1, y1, x2, y2] : null;
}

let measureCtx: CanvasRenderingContext2D | null | undefined;
/**
 * Width of a name pill measured on a canvas (Inter 12/500 + padding + status dot); never narrower
 * than the estimate. Falls back to the estimate where there is no canvas.
 */
export function canvasLabelMeasurer(font: string): (name: string) => number {
  if (measureCtx === undefined) {
    try {
      measureCtx =
        typeof document === 'undefined' ? null : document.createElement('canvas').getContext('2d');
    } catch {
      measureCtx = null;
    }
  }
  const ctx = measureCtx;
  if (!ctx) return estimateLabelWidth;
  const cache = new Map<string, number>();
  return (name) => {
    let w = cache.get(name);
    if (w === undefined) {
      ctx.font = `500 12px ${font}`;
      w = Math.max(Math.ceil(ctx.measureText(name).width) + 34, estimateLabelWidth(name) - 12);
      cache.set(name, w);
    }
    return w;
  };
}

/** The canvas aspect in steps of 0.2 (0.6–2.4), so a few pixels of resize never change the layout. */
export function quantAspect(w: number, h: number): number {
  if (!(w > 0) || !(h > 0)) return 1.6;
  return Math.min(2.4, Math.max(0.6, Math.round((w / h) * 5) / 5));
}
