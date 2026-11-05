// [2b-2] Pure helpers of the switch ports view (architecture.md § 11.5).
import type { HealthItem, Port, PortKind, PortMac } from '../api/types';
import { formatValue } from './format';

// ---------------------------------------------------------------------------
// Tiles

export type TileKind = 'disabled' | 'alert' | 'up' | 'emptyAlert' | 'empty';
export const TILE_KINDS: TileKind[] = ['empty', 'up', 'alert', 'emptyAlert', 'disabled'];
export type TileIcon = 'client' | 'ap' | 'switch' | 'disabled' | null;

/** The legend row of a port (first match, § 11.5 table). */
export function tileKind(p: Pick<Port, 'state' | 'alert'>): TileKind {
  if (p.state === 'admin_down') return 'disabled';
  if (p.state === 'up') return p.alert ? 'alert' : 'up';
  return p.alert ? 'emptyAlert' : 'empty';
}

/** Tailwind classes per tile kind (light / dark), exactly as the contract table. */
export const TILE_CLASS: Record<TileKind, string> = {
  disabled: 'bg-gray-200 text-gray-500 dark:bg-slate-700 dark:text-slate-400',
  alert: 'bg-orange-500 text-white',
  up: 'bg-green-500 text-white',
  emptyAlert: 'bg-orange-200 border border-orange-300 dark:bg-orange-900/40 dark:border-orange-700',
  empty: 'bg-white border border-gray-300 dark:bg-slate-900 dark:border-slate-600',
};

export type TileView = {
  kind: TileKind;
  className: string;
  icon: TileIcon;
  /** small stacked-lines badge at the top-end corner */
  trunk: boolean;
  /** corner count of a MAC-count uplink ("99+" above 99) */
  badge: string | null;
};

export function macCountBadge(n: number): string {
  return n > 99 ? '99+' : String(n);
}

export function tileView(
  p: Pick<Port, 'state' | 'alert' | 'connected_kind' | 'trunk' | 'uplink_reason' | 'mac_count'>,
): TileView {
  const kind = tileKind(p);
  const connected = kind === 'up' || kind === 'alert';
  let icon: TileIcon = null;
  if (kind === 'disabled') icon = 'disabled';
  else if (connected) icon = p.connected_kind === 'none' ? 'client' : p.connected_kind;
  return {
    kind,
    className: TILE_CLASS[kind],
    icon,
    trunk: connected && p.trunk,
    badge: connected && p.uplink_reason === 'mac_count' ? macCountBadge(p.mac_count) : null,
  };
}

// ---------------------------------------------------------------------------
// Formatting

/** 100M / 1G / 2.5G / 10G; null or 0 → null. */
export function formatSpeed(bps: number | null | undefined): string | null {
  if (!bps) return null;
  if (bps >= 1e9) return `${trimNum(bps / 1e9)}G`;
  if (bps >= 1e6) return `${trimNum(bps / 1e6)}M`;
  if (bps >= 1e3) return `${trimNum(bps / 1e3)}k`;
  return String(bps);
}
function trimNum(x: number): string {
  return String(Math.round(x * 10) / 10);
}

/** `n` → "1, 10-12, 20" (runs collapsed). */
export function formatVlanList(vlans: number[]): string {
  const v = [...new Set(vlans)].sort((a, b) => a - b);
  const out: string[] = [];
  for (let i = 0; i < v.length; i++) {
    let j = i;
    while (j + 1 < v.length && v[j + 1] === v[j] + 1) j++;
    out.push(j - i >= 2 ? `${v[i]}-${v[j]}` : j > i ? `${v[i]}, ${v[j]}` : String(v[i]));
    i = j;
  }
  return out.join(', ');
}

export function errorsDelta(p: Pick<Port, 'in_errors_delta' | 'out_errors_delta'>): number | null {
  if (p.in_errors_delta === null && p.out_errors_delta === null) return null;
  return (p.in_errors_delta ?? 0) + (p.out_errors_delta ?? 0);
}

/** The best single text of a MAC: hostname ‖ IP ‖ vendor ‖ MAC. */
export function macSummary(m: PortMac): string {
  return m.hostname || m.ip || m.vendor || m.mac;
}

export type ConnectedView =
  | { kind: 'neighbor'; name: string; device_id: number | null; port: string }
  | { kind: 'uplinkMacs'; count: number }
  | { kind: 'mac'; text: string; more: number; mac: PortMac }
  | { kind: 'none' };

/** Connected column: primary neighbour, else `n MACs` for uplinks, else the first MAC (+k). */
export function connectedView(
  p: Pick<Port, 'neighbor' | 'is_uplink' | 'mac_count' | 'first_mac'>,
): ConnectedView {
  if (p.neighbor)
    return {
      kind: 'neighbor',
      name: p.neighbor.name,
      device_id: p.neighbor.kind === 'device' ? p.neighbor.device_id : null,
      port: p.neighbor.port,
    };
  if (p.is_uplink) return { kind: 'uplinkMacs', count: p.mac_count };
  if (p.first_mac)
    return {
      kind: 'mac',
      text: macSummary(p.first_mac),
      more: Math.max(0, p.mac_count - 1),
      mac: p.first_mac,
    };
  return { kind: 'none' };
}

/** PoE (W) column: watts, else `{on, class}` when delivering, else null ("—"). */
export function poeView(
  p: Pick<Port, 'poe'>,
): { kind: 'watts'; w: number } | { kind: 'on'; cls: number | null } | null {
  if (!p.poe) return null;
  if (p.poe.power_w !== null && p.poe.power_w !== undefined)
    return { kind: 'watts', w: p.poe.power_w };
  if (p.poe.detection === 'delivering') return { kind: 'on', cls: p.poe.power_class };
  return null;
}

/** Health chip text: the formatted value, else `text`, else "—". */
export function healthText(h: Pick<HealthItem, 'value' | 'unit' | 'text'>): string {
  if (h.value !== null && h.value !== undefined) return formatValue(h.value, h.unit);
  return h.text || '—';
}

// ---------------------------------------------------------------------------
// List filter

/** Kinds shown with "Physical ports only" (default on). */
export const PHYSICAL_KINDS: PortKind[] = ['physical', 'lag'];

const IPV4_LIKE = /^\d{1,3}(\.\d{1,3}){0,3}\.?$/;

export function hexDigits(s: string): string {
  return s.toLowerCase().replace(/[^0-9a-f]/g, '');
}

/**
 * A query that may be (part of) a MAC in any notation: hex digits and separators only, with
 * ≥ 6 hex digits, or ≥ 4 with a separator (":", "-", "."); never an IPv4-like query.
 */
export function macQuery(q: string): string | null {
  const s = q.trim();
  if (!/^[0-9a-f:.\-\s]+$/i.test(s) || IPV4_LIKE.test(s)) return null;
  const hex = hexDigits(s);
  // A bare number like a VLAN ("1010") is not a MAC: require ≥ 6 hex digits or a separator.
  if (hex.length < 6 && !(/[:.-]/.test(s) && hex.length >= 4)) return null;
  return hex;
}

function macTexts(m: PortMac): string[] {
  return [m.mac, m.vendor ?? '', m.ip ?? '', ...m.ips, m.hostname ?? '', m.device_name ?? ''];
}

/** Every searchable text of a port (raw fields; the UI adds its translated column texts). */
export function portSearchTexts(p: Port): string[] {
  const out = [
    p.name,
    p.descr,
    p.alias,
    String(p.if_index),
    p.kind,
    p.state,
    p.mode ?? '',
    p.pvid !== null ? String(p.pvid) : '',
    ...p.untagged_vlans.map(String),
    ...p.tagged_vlans.map(String),
    formatSpeed(p.speed_bps) ?? '',
    p.duplex ?? '',
    p.mac ?? '',
  ];
  if (p.neighbor) out.push(p.neighbor.name, p.neighbor.port, p.neighbor.mgmt_address ?? '');
  if (p.is_uplink) out.push(String(p.mac_count));
  const macs = [...p.macs];
  if (p.first_mac && !macs.some((m) => m.mac === p.first_mac?.mac)) macs.push(p.first_mac);
  for (const m of macs) out.push(...macTexts(m));
  return out;
}

function portMacs(p: Port): string[] {
  const all = [p.mac ?? '', ...p.macs.map((m) => m.mac), p.first_mac?.mac ?? ''];
  return all.filter(Boolean).map(hexDigits);
}

/**
 * One filter over all column texts plus every MAC (compared on hex digits, so `0050.5601.0007`
 * finds `00:50:56:01:00:07`), IP, hostname, vendor, neighbour name and alias. Case-insensitive.
 */
export function filterPorts(
  ports: Port[],
  query: string,
  extraTexts?: (p: Port) => string[],
): Port[] {
  const q = query.trim().toLowerCase();
  if (!q) return ports;
  const hex = macQuery(q);
  return ports.filter((p) => {
    if (hex && portMacs(p).some((m) => m.includes(hex))) return true;
    const texts = [...portSearchTexts(p), ...(extraTexts?.(p) ?? [])];
    return texts.some((t) => t && t.toLowerCase().includes(q));
  });
}

// ---------------------------------------------------------------------------
// Columns

export type PortColumn =
  | 'status'
  | 'connected'
  | 'vlan'
  | 'mode'
  | 'speed'
  | 'poe'
  | 'rxtx'
  | 'alias'
  | 'duplex'
  | 'mac'
  | 'ip'
  | 'hostname'
  | 'vendor'
  | 'errors'
  | 'lastChange'
  | 'ifIndex'
  | 'kind';
/** Every choosable column in display order (Port is always shown first and sticky). */
export const PORT_COLUMNS: PortColumn[] = [
  'status',
  'connected',
  'vlan',
  'mode',
  'speed',
  'poe',
  'rxtx',
  'alias',
  'duplex',
  'mac',
  'ip',
  'hostname',
  'vendor',
  'errors',
  'lastChange',
  'ifIndex',
  'kind',
];
export const DEFAULT_COLUMNS: PortColumn[] = [
  'status',
  'connected',
  'vlan',
  'mode',
  'speed',
  'poe',
  'rxtx',
];
export const COLUMNS_KEY = 'netmon.ports.columns';

/** Keeps known columns only, in display order. */
export function normalizeColumns(v: unknown): PortColumn[] | null {
  if (!Array.isArray(v)) return null;
  const set = new Set(v.filter((x): x is PortColumn => PORT_COLUMNS.includes(x as PortColumn)));
  return PORT_COLUMNS.filter((c) => set.has(c));
}

export function loadColumns(
  storage: Pick<Storage, 'getItem'> | null = safeStorage(),
): PortColumn[] {
  try {
    const raw = storage?.getItem(COLUMNS_KEY);
    if (!raw) return DEFAULT_COLUMNS;
    return normalizeColumns(JSON.parse(raw)) ?? DEFAULT_COLUMNS;
  } catch {
    return DEFAULT_COLUMNS;
  }
}

export function saveColumns(
  cols: PortColumn[],
  storage: Pick<Storage, 'setItem'> | null = safeStorage(),
): void {
  try {
    storage?.setItem(COLUMNS_KEY, JSON.stringify(normalizeColumns(cols) ?? DEFAULT_COLUMNS));
  } catch {
    /* storage blocked or full: the choice lasts for this page only */
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function toggleColumn(cols: PortColumn[], c: PortColumn, on: boolean): PortColumn[] {
  const set = new Set(cols);
  if (on) set.add(c);
  else set.delete(c);
  return PORT_COLUMNS.filter((x) => set.has(x));
}

// ---------------------------------------------------------------------------
// Find page input

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

export function isIpv4(s: string): boolean {
  return IPV4.test(s.trim());
}

/**
 * The search request for an input: IPv4 → `{ip}`, else `{q}` when it is a MAC or a prefix of
 * 6–12 hex digits after removing separators; null when invalid (the API would answer 422).
 */
export function parseSearchInput(input: string): { q: string } | { ip: string } | null {
  const s = input.trim();
  if (!s) return null;
  if (isIpv4(s)) return { ip: s };
  if (!/^[0-9a-f:.\-\s]+$/i.test(s)) return null;
  const hex = hexDigits(s);
  if (hex.length < 6 || hex.length > 12) return null;
  return { q: s.replace(/\s+/g, '') };
}

/** The port view link of a search hit (opens the port modal on the switch's page). */
export function portLink(x: { device_id: number; if_index: number }): string {
  return `/devices/${x.device_id}?tab=ports&port=${x.if_index}`;
}

/** "aabbccddeeff" → "aa:bb:cc:dd:ee:ff" (for display of prefixes too). */
export function colonMac(hex: string): string {
  return (hex.match(/.{1,2}/g) ?? []).join(':');
}

// ---------------------------------------------------------------------------
// Settings

/** [2b-2] Port inventory: interval, uplink MAC threshold, MAC/ARP retention, reverse DNS. */
export function portSettingsErrors(v: { interval: string; threshold: string; retention: string }): {
  interval: boolean;
  threshold: boolean;
  retention: boolean;
} {
  const int = (s: string) => (/^\d+$/.test(s.trim()) ? Number(s) : NaN);
  const i = int(v.interval);
  const th = int(v.threshold);
  const r = int(v.retention);
  return {
    interval: !(i === 0 || (i >= 5 && i <= 1440)),
    threshold: !(th >= 1 && th <= 1000),
    retention: !(r >= 1 && r <= 365),
  };
}
