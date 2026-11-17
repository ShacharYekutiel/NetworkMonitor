// [premium] Home "network at a glance" map: a generic tree layout from parent_id (root at the top,
// chains downward, leaves in a row) and small helpers of the home screen. Pure, unit-tested.
import type { MapLinkStatus, OverviewMapLink, OverviewMapNode } from '../api/premium';

export const MAP_TILE = 64;
/**
 * Height of the label block under a tile (HomeMap NodeTile): 7px margin + 3px gap + the 20px name
 * line + 3px gap + the 16px caption line. Links start below it, never through the caption.
 */
export const MAP_LABEL = 50;

export type PlacedNode = { node: OverviewMapNode; x: number; y: number; depth: number };
export type PlacedEdge = { from: PlacedNode; to: PlacedNode; status: MapLinkStatus; d: string };
export type MapLayout = { nodes: PlacedNode[]; edges: PlacedEdge[]; width: number; height: number };

/**
 * Lays the map out as a tidy tree: every leaf gets an equal slot in DFS order, a parent sits over
 * the middle of its children, each depth is a row. `minWidth` is the hero's width; with many leaves
 * the map grows wider (`colWidth` per leaf) and the hero scrolls sideways.
 */
export function layoutMap(
  nodes: OverviewMapNode[],
  links: OverviewMapLink[],
  {
    minWidth,
    colWidth = 132,
    rowHeight = 160,
    top = 48,
  }: {
    minWidth: number;
    colWidth?: number;
    rowHeight?: number;
    top?: number;
  },
): MapLayout {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const children = new Map<string, OverviewMapNode[]>();
  const roots: OverviewMapNode[] = [];
  for (const n of nodes) {
    if (n.parent_id !== null && n.parent_id !== n.id && byId.has(n.parent_id)) {
      const list = children.get(n.parent_id) ?? [];
      list.push(n);
      children.set(n.parent_id, list);
    } else roots.push(n);
  }
  // Walk once (guarding against cycles): depth and the tree actually walked.
  const seen = new Set<string>();
  const order: { node: OverviewMapNode; depth: number }[] = [];
  const kidsOf = new Map<string, string[]>();
  const walk = (n: OverviewMapNode, depth: number) => {
    seen.add(n.id);
    order.push({ node: n, depth });
    const kids: string[] = [];
    for (const k of children.get(n.id) ?? []) {
      if (seen.has(k.id)) continue;
      kids.push(k.id);
      walk(k, depth + 1);
    }
    kidsOf.set(n.id, kids);
  };
  for (const r of roots) if (!seen.has(r.id)) walk(r, 0);
  // Nodes only reachable through a cycle become extra roots.
  for (const n of nodes) if (!seen.has(n.id)) walk(n, 0);

  const leaves = order.filter((o) => !kidsOf.get(o.node.id)?.length).length || 1;
  const width = Math.max(minWidth, leaves * colWidth);
  const slot = width / leaves;
  const maxDepth = order.reduce((m, o) => Math.max(m, o.depth), 0);
  const depthOf = new Map(order.map((o) => [o.node.id, o.depth]));
  const placed = new Map<string, PlacedNode>();
  let leafIndex = 0;
  // DFS in walk order: leaves take the next slot, a parent centres over its children.
  const place = (id: string): PlacedNode => {
    const kids = kidsOf.get(id) ?? [];
    let x: number;
    if (!kids.length) x = (leafIndex++ + 0.5) * slot;
    else {
      const xs = kids.map((k) => place(k).x);
      x = (Math.min(...xs) + Math.max(...xs)) / 2;
    }
    const depth = depthOf.get(id) ?? 0;
    const p = { node: byId.get(id)!, x, y: top + MAP_TILE / 2 + depth * rowHeight, depth };
    placed.set(id, p);
    return p;
  };
  for (const o of order) if (o.depth === 0) place(o.node.id);

  const linkStatus = new Map<string, MapLinkStatus>();
  for (const l of links) {
    linkStatus.set(`${l.from}>${l.to}`, l.status);
    if (!linkStatus.has(`${l.to}>${l.from}`)) linkStatus.set(`${l.to}>${l.from}`, l.status);
  }
  const edges: PlacedEdge[] = [];
  for (const o of order) {
    const pid = o.node.parent_id;
    if (pid === null || !placed.has(pid) || o.depth === 0) continue;
    const from = placed.get(pid)!;
    const to = placed.get(o.node.id)!;
    edges.push({
      from,
      to,
      status: linkStatus.get(`${pid}>${o.node.id}`) ?? 'unknown',
      d: edgePath(from, to),
    });
  }
  // Broken links first, so the healthy lines are drawn on top of them.
  const rank = (x: MapLinkStatus) =>
    x === 'down' || x === 'warning' ? 0 : x === 'unknown' ? 1 : 2;
  edges.sort((a, b) => rank(a.status) - rank(b.status));
  return {
    nodes: order.map((o) => placed.get(o.node.id)!),
    edges,
    width,
    height: top + MAP_TILE + MAP_LABEL + maxDepth * rowHeight + 24,
  };
}

/** Parent bottom (under its caption) -> a bus line above the child -> child top, rounded corners. */
export function edgePath(a: { x: number; y: number }, b: { x: number; y: number }): string {
  const y0 = a.y + MAP_TILE / 2 + MAP_LABEL + 6;
  const y1 = b.y - MAP_TILE / 2 - 6;
  if (Math.abs(a.x - b.x) < 0.5) return `M${r1(a.x)} ${r1(y0)}V${r1(y1)}`;
  const my = y1 - 18;
  const dir = Math.sign(b.x - a.x);
  const r = Math.min(8, Math.abs(b.x - a.x) / 2, Math.max(0, my - y0));
  return (
    `M${r1(a.x)} ${r1(y0)}V${r1(my - r)}` +
    `Q${r1(a.x)} ${r1(my)} ${r1(a.x + dir * r)} ${r1(my)}` +
    `H${r1(b.x - dir * r)}Q${r1(b.x)} ${r1(my)} ${r1(b.x)} ${r1(my + r)}V${r1(y1)}`
  );
}
const r1 = (v: number) => Math.round(v * 10) / 10;

/** Time in a state as a running clock: "4:02", "1:03:11"; past 99 hours: whole days. */
export function elapsed(
  since: string | null | undefined,
  now: number,
): { clock: string } | { days: number } | null {
  if (!since) return null;
  const t = Date.parse(since);
  if (Number.isNaN(t)) return null;
  const s = Math.max(0, Math.floor((now - t) / 1000));
  const h = Math.floor(s / 3600);
  if (h > 99) return { days: Math.floor(h / 24) };
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return { clock: h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}` };
}

export type MapIconKey =
  'internet' | 'firewall' | 'switch' | 'server' | 'wifi' | 'power' | 'branch' | 'group';

/** Icon of a map node: by role; a group by a keyword of its name, else a folder. */
export function mapIcon(n: Pick<OverviewMapNode, 'id' | 'kind' | 'role' | 'name'>): MapIconKey {
  if (n.role === 'internet' || n.kind === 'internet') return 'internet';
  if (n.id === 'root') return 'branch';
  if (n.id === 'more') return 'group';
  if (n.role === 'firewall') return 'firewall';
  if (n.role === 'core' || n.kind === 'device') return 'switch';
  const s = n.name.toLowerCase();
  if (/wi-?fi|wireless|wlan|\bap\b|אלחוט/.test(s)) return 'wifi';
  if (/server|\bsrv|dc\b|datacent|שרת/.test(s)) return 'server';
  if (/\bups\b|power|חשמל/.test(s)) return 'power';
  if (/branch|site|office|סניף|סניפ|אתר/.test(s)) return 'branch';
  if (/access|core|switch|distrib|גישה|ליבה|מתג/.test(s)) return 'switch';
  return 'group';
}
