// Design tokens as literal colours for ECharts and inline SVG, which cannot read CSS variables
// (docs/design-system/IMPLEMENTATION.md). Values are read from <html> with getComputedStyle;
// the tables below (copied from src/styles/tokens.css) are the fallback when the variables are
// unavailable (tests) or the requested theme is not the one applied yet.
import type { Status } from '../api/types';

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
const FONTS = {
  fontSans: '"Barlow", "Heebo", "Segoe UI", system-ui, sans-serif',
  fontMono: '"JetBrains Mono", ui-monospace, Consolas, monospace',
  fontDisplay: '"Barlow Condensed", "Heebo", "Arial Narrow", sans-serif',
};

export const LIGHT_TOKENS: ChartTokens = {
  bg: '#f2f0e9',
  surface: '#fbfaf6',
  surfaceRaised: '#ffffff',
  surfaceSunken: '#e9e6dc',
  border: '#d6d2c4',
  borderStrong: '#a9a493',
  ink: '#14150f',
  inkMuted: '#4f5044',
  inkSubtle: '#6c6d60',
  accent: '#3f6b12',
  accentInk: '#3f6b12',
  accentSoft: '#e7f0d6',
  onAccent: '#ffffff',
  onStatus: '#ffffff',
  ack: '#0f7f74',
  chart1: '#5a8a00',
  chart2: '#0077b6',
  chart3: '#c2338f',
  chart4: '#1f8a4c',
  chartGrid: '#e2dfd4',
  status: {
    up: '#1f8a4c',
    warning: '#a35f00',
    down: '#d03a24',
    unreachable: '#6b4fd8',
    unknown: '#5f6155',
    paused: '#6c6e62',
  },
  statusSoft: {
    up: '#dcf1e3',
    warning: '#fbecd0',
    down: '#fbe0da',
    unreachable: '#ebe5fd',
    unknown: '#e9e7df',
    paused: '#eeece5',
  },
  ...FONTS,
};

export const DARK_TOKENS: ChartTokens = {
  bg: '#0c0d0a',
  surface: '#141511',
  surfaceRaised: '#1c1d18',
  surfaceSunken: '#090a08',
  border: '#2a2c24',
  borderStrong: '#3d4035',
  ink: '#ecebe4',
  inkMuted: '#a3a395',
  inkSubtle: '#85867a',
  accent: '#b6e35a',
  accentInk: '#b6e35a',
  accentSoft: '#1f2612',
  onAccent: '#11140b',
  onStatus: '#0c0d0a',
  ack: '#4fd6c8',
  chart1: '#c4f03c',
  chart2: '#5cc8ff',
  chart3: '#ff8fd1',
  chart4: '#4fd68a',
  chartGrid: '#22241d',
  status: {
    up: '#4fd68a',
    warning: '#ffb627',
    down: '#ff5c49',
    unreachable: '#b195ff',
    unknown: '#8d8f82',
    paused: '#808276',
  },
  statusSoft: {
    up: '#12251a',
    warning: '#2c2210',
    down: '#331410',
    unreachable: '#221c36',
    unknown: '#1b1c17',
    paused: '#171813',
  },
  ...FONTS,
};

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
  const read = (name: string, fb: string) => cs.getPropertyValue(`--${name}`).trim() || fb;
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

/** Colour of a status (unknown for anything else). */
export function statusColor(tk: ChartTokens, s: string | null | undefined): string {
  return (tk.status as Record<string, string>)[s ?? 'unknown'] ?? tk.status.unknown;
}
