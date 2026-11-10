// [3] Flow views helpers (architecture.md § 13.3.7, api.md § [3] Flows). Pure.
import type { FlowCounters, FlowExporter, FlowSeries } from '../api/types3';
import { FLOW_DROP_REASONS } from '../api/types3';

export const FLOW_PRESETS = ['15m', '1h', '6h', '24h', '7d', '31d'] as const;
export type FlowPreset = (typeof FLOW_PRESETS)[number];
const PRESET_MS: Record<FlowPreset, number> = {
  '15m': 15 * 60_000,
  '1h': 3_600_000,
  '6h': 6 * 3_600_000,
  '24h': 24 * 3_600_000,
  '7d': 7 * 86_400_000,
  '31d': 31 * 86_400_000,
};
export const FLOW_MAX_RANGE_MS = 31 * 86_400_000;
export const CONVERSATION_MAX_RANGE_MS = 24 * 3_600_000;

/** since/until of a preset, rounded down to the minute (stable query keys within a minute). */
export function flowRange(preset: FlowPreset, now: number): { since: string; until: string } {
  const until = Math.floor(now / 60_000) * 60_000;
  return {
    since: new Date(until - PRESET_MS[preset]).toISOString(),
    until: new Date(until).toISOString(),
  };
}

export function presetMs(preset: FlowPreset): number {
  return PRESET_MS[preset];
}

/** Conversations are limited to 24 h (else the API answers 422). */
export function conversationsAllowed(preset: FlowPreset): boolean {
  return PRESET_MS[preset] <= CONVERSATION_MAX_RANGE_MS;
}

const SI = ['', 'k', 'M', 'G', 'T', 'P'];

function scale(v: number, base: number): [number, number] {
  let i = 0;
  let x = v;
  while (Math.abs(x) >= base && i < SI.length - 1) {
    x /= base;
    i++;
  }
  return [x, i];
}

function fmt(x: number): string {
  const digits = Math.abs(x) < 10 ? 2 : Math.abs(x) < 100 ? 1 : 0;
  return x.toFixed(digits).replace(/\.0+$|(\.\d*?)0+$/, '$1');
}

/** Decimal bytes: "1.5 GB" (flow volumes). */
export function formatBytes(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const [x, i] = scale(v, 1000);
  return `${fmt(x)} ${SI[i]}B`;
}

/** Rates: "12.5 Mbit/s". */
export function formatBits(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const [x, i] = scale(v, 1000);
  return `${fmt(x)} ${SI[i]}bit/s`;
}

/** Plain counts with k/M: "12.3k". */
export function formatCount(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (Math.abs(v) < 1000) return String(Math.round(v));
  const [x, i] = scale(v, 1000);
  return `${fmt(x)}${SI[i]}`;
}

export function formatShare(share: number): string {
  return `${(share * 100).toFixed(share < 0.1 ? 1 : 0)}%`;
}

export function totalDropped(c: FlowCounters | null | undefined): number {
  if (!c) return 0;
  return FLOW_DROP_REASONS.reduce((s, k) => s + (c.dropped[k] ?? 0), 0);
}

/** Effective sampling rate of an exporter (override wins, else what it announced, else 1). */
export function effectiveSampling(
  e: Pick<FlowExporter, 'sampling_override' | 'sampling_seen'>,
): number {
  return e.sampling_override ?? e.sampling_seen ?? 1;
}

/** null when OK, else an i18n key: empty = no override, else an integer 1–1 000 000. */
export function samplingError(v: string): string | null {
  const s = v.trim();
  if (!s) return null;
  if (!/^\d+$/.test(s)) return 'exporter.samplingInvalid';
  const n = Number(s);
  return n >= 1 && n <= 1_000_000 ? null : 'exporter.samplingInvalid';
}

export type ChartLine = {
  name: string;
  key: string;
  direction: 'in' | 'out';
  data: [number, number][];
};

/**
 * Interface traffic for stacked areas: "in" above the axis, "out" mirrored below (negative), so
 * both directions are readable in one chart. Points are [ms, bits/s].
 */
export function mirroredSeries(s: FlowSeries | null | undefined): ChartLine[] {
  if (!s) return [];
  return s.series.map((x) => ({
    name: x.label,
    key: x.key,
    direction: x.direction,
    data: x.points.map(([t, v]) => [Date.parse(t), x.direction === 'out' ? -v : v]),
  }));
}

/** Peak of a direction (bits/s) over all its series summed per bucket. */
export function peakRate(s: FlowSeries | null | undefined, direction: 'in' | 'out'): number {
  if (!s) return 0;
  const sums = new Map<string, number>();
  for (const x of s.series)
    if (x.direction === direction)
      for (const [t, v] of x.points) sums.set(t, (sums.get(t) ?? 0) + v);
  let max = 0;
  for (const v of sums.values()) max = Math.max(max, v);
  return max;
}

/** "tcp/443" → "TCP 443" style port label for applications without a known name. */
export function protocolName(p: number | undefined): string {
  switch (p) {
    case 6:
      return 'TCP';
    case 17:
      return 'UDP';
    case 1:
      return 'ICMP';
    case 58:
      return 'ICMPv6';
    case 47:
      return 'GRE';
    case 50:
      return 'ESP';
    default:
      return p === undefined ? '' : `IP ${p}`;
  }
}
