// [2b-1] Topology map: graph builder (API → ECharts graph data) and dependency suggestions (pure).
import type {
  DependencyBulkItem,
  DependencySuggestion,
  DeviceClass,
  LinkEnd,
  Status,
  TopologyGraph,
  TopologyLink,
  TopologyNode,
} from '../api/types';
import { formatValue } from './format';
import { severity } from './status';
import { DARK_TOKENS, LIGHT_TOKENS, chartTokens, statusColor } from './chartTokens';

/** Inner SVG markup (24×24 stroke icons) per device class, same shapes as components/icons. */
const CLASS_SVG: Record<DeviceClass, string> = {
  router:
    '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 16.5h.01M11 16.5h.01M17 13V9M14 7a4 4 0 016 0M12 5a7 7 0 0110 0"/>',
  switch:
    '<rect x="2" y="7" width="20" height="10" rx="2"/><path d="M6 11h2v2H6zM10 11h2v2h-2zM14 11h2v2h-2zM18 11h.01"/>',
  access_point:
    '<path d="M2 9a15 15 0 0120 0M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0"/><circle cx="12" cy="19" r="1"/>',
  printer:
    '<path d="M6 9V3h12v6"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M6 14h12v7H6z"/>',
  nas: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 7h8M8 11h8M8 15h8"/>',
  server:
    '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  workstation: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  // not detected yet: the generic device tile of the home map (a "?" read as an error)
  unknown:
    '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 12h.01M10 12h.01M13 12h.01M16 12h.01"/>',
};
const WRENCH =
  '<path d="M14.7 6.3a4 4 0 00-5.4 5.2L3.6 17.2a1.6 1.6 0 002.2 2.2l5.7-5.7a4 4 0 005.2-5.4l-2.5 2.5-2.1-.5-.5-2.1z"/>';

/** Links without an interface sensor: the `border-strong` token. */
export const NEUTRAL_EDGE = { light: LIGHT_TOKENS.borderStrong, dark: DARK_TOKENS.borderStrong };

const SHIELD =
  '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>';
/** A firewall is recognised by its name, as on the home map and the group page. */
const FW_NAME = /(^|[^a-z])(fw|firewall|fortigate|asa|pfsense|palo)([^a-z]|$)/i;
/** States that draw a ring and colour the icon (status colour only where something is wrong). */
const RING: ReadonlySet<Status> = new Set(['warning', 'down', 'unreachable', 'paused']);

/**
 * Device node symbol (premium BUILD.md § 6 "Topology": the quiet tiles of the home map): a 56px
 * rounded tile on `surface` with a soft shadow and the class icon. A problem adds a 2px ring and a
 * soft halo in the status colour and colours the icon; the selected node is an inverted (ink) tile;
 * maintenance adds the wrench badge.
 */
export function deviceSymbol(
  status: Status,
  cls: DeviceClass | null,
  maintenance: boolean,
  dark: boolean,
  opts: { name?: string; selected?: boolean } = {},
): string {
  const tk = chartTokens(dark);
  const color = statusColor(tk, status);
  const ring = RING.has(status);
  const icon = FW_NAME.test(opts.name ?? '')
    ? SHIELD
    : (CLASS_SVG[cls ?? 'unknown'] ?? CLASS_SVG.unknown);
  const fill = opts.selected ? tk.ink : tk.surface;
  const ink = opts.selected ? tk.surface : ring ? color : tk.ink;
  const badge = maintenance
    ? `<circle cx="13" cy="13" r="8" fill="${tk.accent}" stroke="${tk.surface}" stroke-width="2"/>` +
      `<g transform="translate(7.4 7.4) scale(0.47)" fill="none" stroke="${tk.onAccent}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">${WRENCH}</g>`
    : '';
  const shadow = dark ? '0.55' : '0.13';
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72">` +
    `<defs><filter id="s" x="-30%" y="-30%" width="160%" height="170%"><feDropShadow dx="0" dy="3" stdDeviation="3.5" flood-color="#000" flood-opacity="${shadow}"/></filter></defs>` +
    (ring
      ? `<rect x="4" y="4" width="64" height="64" rx="22" fill="none" stroke="${color}" stroke-opacity="0.16" stroke-width="8"/>`
      : '') +
    `<rect x="8" y="8" width="56" height="56" rx="18" fill="${fill}" filter="url(#s)"` +
    (ring ? ` stroke="${color}" stroke-width="2"` : '') +
    `/>` +
    `<g transform="translate(25 25) scale(0.9167)" fill="none" stroke="${ink}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${icon}</g>` +
    badge +
    `</svg>`;
  return `image://data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** Unmanaged neighbour: a quiet dashed tile with a question mark, no shadow. */
export function ghostSymbol(dark: boolean, selected = false): string {
  const tk = chartTokens(dark);
  const stroke = tk.borderStrong;
  const fill = selected ? tk.ink : tk.surfaceSunken;
  const ink = selected ? tk.surface : tk.inkSubtle;
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72">` +
    `<rect x="12" y="12" width="48" height="48" rx="16" fill="${fill}" stroke="${stroke}" stroke-width="1.5" stroke-dasharray="5 4"/>` +
    `<g transform="translate(26 26) scale(0.8333)" fill="none" stroke="${ink}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 9.5a2.5 2.5 0 015 .5c0 1.5-2.5 2-2.5 3.5M12 17h.01"/></g>` +
    `</svg>`;
  return `image://data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/**
 * State of one link end from its interface sensor: `oper_status` 1 → up, any other value →
 * down; no reading yet → the sensor status (else unknown); no interface sensor → null.
 */
export function endState(e: LinkEnd): Status | null {
  if (e.sensor_id === null) return null;
  if (e.sensor_status === 'paused') return 'paused';
  if (e.oper_status === null || e.oper_status === undefined) return e.sensor_status ?? 'unknown';
  return e.oper_status === 1 ? 'up' : 'down';
}

/** Worst state of the two ends (null when neither end has an interface sensor). */
export function linkState(l: TopologyLink): Status | null {
  const states = [endState(l.a), endState(l.b)].filter((s): s is Status => s !== null);
  if (!states.length) return null;
  return states.reduce((w, s) => (severity(s) > severity(w) ? s : w));
}

export function linkColor(l: TopologyLink, dark: boolean): string {
  const s = linkState(l);
  return s ? statusColor(chartTokens(dark), s) : chartTokens(dark).borderStrong;
}

/** The end that carries traffic data (the first with an interface sensor and a rate). */
export function trafficEnd(l: TopologyLink): LinkEnd | null {
  for (const e of [l.a, l.b])
    if (e.sensor_id !== null && (e.in_bps !== null || e.out_bps !== null)) return e;
  return null;
}

/** "↓ 12.3 Mbit/s ↑ 1.2 Mbit/s" of the traffic end, or '' when there is none. */
export function trafficLabel(l: TopologyLink): string {
  const e = trafficEnd(l);
  if (!e) return '';
  return `↓ ${formatValue(e.in_bps, 'bit/s')}  ↑ ${formatValue(e.out_bps, 'bit/s')}`;
}

export type GraphNodeDatum = {
  id: string;
  name: string;
  kind: TopologyNode['kind'];
  /** device id or neighbour id */
  refId: number;
  status: Status | null;
  maintenance: boolean;
  ghost: boolean;
  inFilter: boolean;
  x?: number;
  y?: number;
  fixed?: boolean;
  symbol: string;
  symbolSize: number;
  value: number;
  itemStyle: { opacity: number };
  /** the name pill under the tile: the name and a status dot (rich text) */
  label: { color: string; formatter?: string; rich?: Record<string, Record<string, unknown>> };
};

export type GraphLinkDatum = {
  id: string;
  linkId: number;
  source: string;
  target: string;
  state: Status | null;
  stale: boolean;
  lineStyle: { color: string; width: number; type: 'solid' | 'dashed'; opacity: number };
  label: { show: boolean; formatter: string; color: string };
};

export type BuiltGraph = {
  nodes: GraphNodeDatum[];
  links: GraphLinkDatum[];
  /** true when some node has no stored position (a one-off force layout is needed). */
  needsLayout: boolean;
};

/**
 * API graph → ECharts graph series data. `positions` (e.g. local drags not yet refetched)
 * override the API x/y. Links whose endpoints are missing from `nodes` are dropped.
 */
export function buildGraph(
  g: Pick<TopologyGraph, 'nodes' | 'links'>,
  opts: {
    dark: boolean;
    showTraffic: boolean;
    positions?: Record<string, [number, number]>;
    /** key of the selected node: drawn as an inverted (ink) tile */
    selected?: string | null;
  },
): BuiltGraph {
  const { dark, showTraffic } = opts;
  const tk = chartTokens(dark);
  const keys = new Set(g.nodes.map((n) => n.key));
  const degree = new Map<string, number>();
  const links: GraphLinkDatum[] = [];
  for (const l of g.links) {
    if (!keys.has(l.a.node) || !keys.has(l.b.node)) continue;
    degree.set(l.a.node, (degree.get(l.a.node) ?? 0) + 1);
    degree.set(l.b.node, (degree.get(l.b.node) ?? 0) + 1);
    const label = showTraffic ? trafficLabel(l) : '';
    links.push({
      id: `l${l.id}`,
      linkId: l.id,
      source: l.a.node,
      target: l.b.node,
      state: linkState(l),
      stale: l.stale,
      lineStyle: {
        color: linkColor(l, dark),
        // premium: hairline links (1.5px), a broken link 2px red dashed
        width: linkState(l) === 'down' ? 2 : 1.5,
        // Down links are dashed too (status never by colour alone); stale ones also fade.
        type: l.stale || linkState(l) === 'down' ? 'dashed' : 'solid',
        opacity: l.stale ? 0.4 : 0.85,
      },
      label: { show: !!label, formatter: label, color: tk.inkMuted },
    });
  }
  const text = tk.ink;
  let needsLayout = false;
  const nodes = g.nodes.map((n): GraphNodeDatum => {
    const pos = opts.positions?.[n.key] ?? (n.x !== null && n.y !== null ? [n.x, n.y] : null);
    if (!pos) needsLayout = true;
    const base = {
      id: n.key,
      name: n.name,
      kind: n.kind,
      value: degree.get(n.key) ?? 0,
      ...(pos ? { x: pos[0], y: pos[1], fixed: true } : {}),
    };
    if (n.kind === 'device')
      return {
        ...base,
        refId: n.device_id,
        status: n.status,
        maintenance: n.in_maintenance,
        ghost: false,
        inFilter: n.in_filter,
        symbol: deviceSymbol(n.status, n.device_class, n.in_maintenance, dark, {
          name: n.name,
          selected: opts.selected === n.key,
        }),
        symbolSize: 72,
        itemStyle: { opacity: n.in_filter ? 1 : 0.45 },
        label: {
          color: text,
          // "{n|name}{d|}": the dot is a 7px status swatch next to the name (prototype .nm i)
          formatter: '{n|{b}}{d|}',
          rich: {
            d: { width: 7, height: 7, borderRadius: 4, backgroundColor: statusColor(tk, n.status) },
          },
        },
      };
    return {
      ...base,
      refId: n.neighbor_id,
      status: null,
      maintenance: false,
      ghost: true,
      inFilter: true,
      symbol: ghostSymbol(dark, opts.selected === n.key),
      symbolSize: 72,
      itemStyle: { opacity: 0.9 },
      label: { color: tk.inkSubtle },
    };
  });
  return { nodes, links, needsLayout };
}

/**
 * Whether a node mouseup ends a real drag: the pointer moved more than `thresholdPx`
 * pixels since mousedown and the node's layout position changed. A plain click → false.
 */
export function isRealDrag(
  downPx: [number, number],
  upPx: [number, number],
  before: [number, number] | null,
  after: [number, number] | null,
  thresholdPx = 3,
): boolean {
  if (!after) return false;
  if (Math.hypot(upPx[0] - downPx[0], upPx[1] - downPx[1]) <= thresholdPx) return false;
  if (before && Math.abs(before[0] - after[0]) < 0.5 && Math.abs(before[1] - after[1]) < 0.5)
    return false;
  return true;
}

/** Force repulsion scaled by node count (denser graphs need more room). */
export function forceRepulsion(nodeCount: number): number {
  return Math.round(Math.min(1200, 180 + nodeCount * 18));
}

// ---------------------------------------------------------------------------
// Dependency suggestions

export type SuggestionRow = DependencySuggestion & {
  /** applying would change the stored dependency */
  changes: boolean;
  /** the suggestion already equals the effective parent */
  alreadyEffective: boolean;
};

export function suggestionRows(items: DependencySuggestion[]): SuggestionRow[] {
  return items.map((s) => ({
    ...s,
    changes:
      s.suggested_parent_id !== null &&
      !(s.current.mode === 'device' && s.current.device_id === s.suggested_parent_id),
    alreadyEffective:
      s.suggested_parent_id !== null && s.effective_parent_id === s.suggested_parent_id,
  }));
}

/**
 * Rows selected by default: only devices that inherit and have no effective parent yet.
 * An explicit choice ("none" or a device) is never preselected.
 */
export function defaultSelection(rows: SuggestionRow[]): Set<number> {
  return new Set(
    rows
      .filter(
        (r) =>
          r.suggested_parent_id !== null &&
          r.current.mode === 'inherit' &&
          r.effective_parent_id === null,
      )
      .map((r) => r.device_id),
  );
}

/** Bulk body for `POST /api/devices/dependencies` (explicit device dependency, automatic sensor). */
export function suggestionsToBulk(
  rows: SuggestionRow[],
  selected: Set<number>,
): DependencyBulkItem[] {
  return rows
    .filter((r) => selected.has(r.device_id) && r.suggested_parent_id !== null && r.changes)
    .map((r) => ({
      device_id: r.device_id,
      dependency: { mode: 'device', device_id: r.suggested_parent_id, sensor_id: null },
    }));
}
