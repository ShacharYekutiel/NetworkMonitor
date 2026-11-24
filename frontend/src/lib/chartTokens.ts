// Design tokens as literal colours for ECharts and inline SVG, which cannot read CSS variables
// (docs/design-system/IMPLEMENTATION.md). Values are read from <html> with getComputedStyle;
// the tables below are the fallback when the variables are
// unavailable (tests) or the requested theme is not the one applied yet; they come from the
// generated token table, so they never drift from tokens.json.
import type { Status } from '../api/types';
import { TOKEN_CONSTANTS, TOKEN_VALUES } from '../styles/tokens.gen';

export type ChartTokens = {
  bg: string;
  surface: string;
  surfaceRaised: string;
  surfaceSunken: string;
  border: string;
  borderStrong: string;
  ink: string;
  inkMuted: string;
  inkSubtle: string;
  accent: string;
  accentInk: string;
  accentSoft: string;
  onAccent: string;
  onStatus: string;
  ack: string;
  chart1: string;
  chart2: string;
  chart3: string;
  chart4: string;
  chartGrid: string;
  status: Record<Status, string>;
  statusSoft: Record<Status, string>;
  fontSans: string;
  fontMono: string;
  fontDisplay: string;
};

const VARS: Record<Exclude<keyof ChartTokens, 'status' | 'statusSoft'>, string> = {
  bg: 'bg',
  surface: 'surface',
  surfaceRaised: 'surface-raised',
  surfaceSunken: 'surface-sunken',
  border: 'border',
  borderStrong: 'border-strong',
  ink: 'ink',
  inkMuted: 'ink-muted',
  inkSubtle: 'ink-subtle',
  accent: 'accent',
  accentInk: 'accent-ink',
  accentSoft: 'accent-soft',
  onAccent: 'on-accent',
  onStatus: 'on-status',
  ack: 'ack',
  chart1: 'chart-1',
  chart2: 'chart-2',
  chart3: 'chart-3',
  chart4: 'chart-4',
  chartGrid: 'chart-grid',
  fontSans: 'font-sans',
  fontMono: 'font-mono',
  fontDisplay: 'font-display',
};

const STATUSES: Status[] = ['up', 'warning', 'down', 'unreachable', 'unknown', 'paused'];

/** The literal values of one theme, from the generated token table (src/styles/tokens.gen.ts). */
function fromTable(theme: 'light' | 'dark'): ChartTokens {
  const t = TOKEN_VALUES[theme] as Record<string, string>;
  const c = TOKEN_CONSTANTS as Record<string, string>;
  const out = {} as ChartTokens;
  for (const [k, v] of Object.entries(VARS) as [keyof typeof VARS, string][]) {
    out[k] = t[v] ?? c[v];
  }
  out.status = {} as Record<Status, string>;
  out.statusSoft = {} as Record<Status, string>;
  for (const s of STATUSES) {
    out.status[s] = t[`status-${s}`];
    out.statusSoft[s] = t[`status-${s}-soft`];
  }
  return out;
}

export const LIGHT_TOKENS: ChartTokens = fromTable('light');
export const DARK_TOKENS: ChartTokens = fromTable('dark');

/**
 * The token values of the `dark` or light theme. Read live from <html> when that theme is the
 * one applied (so a changed tokens.css wins), else from the tables above.
 */
export function chartTokens(dark: boolean): ChartTokens {
  const fallback = dark ? DARK_TOKENS : LIGHT_TOKENS;
  if (typeof document === 'undefined' || typeof getComputedStyle === 'undefined') return fallback;
  const root = document.documentElement;
  if (root.classList.contains('dark') !== dark) return fallback;
  const cs = getComputedStyle(root);
  const read = (name: string, fb: string) =>
    canvasColor(cs.getPropertyValue(`--${name}`).trim()) || fb;
  const out = {
    ...fallback,
    status: { ...fallback.status },
    statusSoft: { ...fallback.statusSoft },
  };
  for (const [k, v] of Object.entries(VARS) as [keyof typeof VARS, string][]) {
    out[k] = read(v, fallback[k]);
  }
  for (const s of STATUSES) {
    out.status[s] = read(`status-${s}`, fallback.status[s]);
    out.statusSoft[s] = read(`status-${s}-soft`, fallback.statusSoft[s]);
  }
  return out;
}

/**
 * ECharts/zrender cannot parse #rgba / #rrggbbaa, which the CSS minifier makes of the rgba() tokens
 * (`rgba(0, 0, 0, 0.08)` -> `#00000014`): turn those back into rgba().
 */
export function canvasColor(v: string): string {
  const m = /^#([0-9a-f]{4}|[0-9a-f]{8})$/i.exec(v);
  if (!m) return v;
  const h = m[1].length === 4 ? [...m[1]].map((c) => c + c).join('') : m[1];
  const n = [0, 2, 4, 6].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgba(${n[0]}, ${n[1]}, ${n[2]}, ${Math.round((n[3] / 255) * 1000) / 1000})`;
}

/** Colour of a status (unknown for anything else). */
export function statusColor(tk: ChartTokens, s: string | null | undefined): string {
  return (tk.status as Record<string, string>)[s ?? 'unknown'] ?? tk.status.unknown;
}
