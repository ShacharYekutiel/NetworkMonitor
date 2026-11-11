import type { Status, StatusCounts } from '../api/types';

/** Every status, worst first (data-model.md § 2b-1 status severity). */
export const STATUSES: Status[] = ['down', 'warning', 'unreachable', 'unknown', 'up', 'paused'];

/** Severity per data-model.md [2b-1]: down > warning > unreachable > unknown > up > paused. */
export const SEVERITY: Record<Status, number> = {
  down: 5,
  warning: 4,
  unreachable: 3,
  unknown: 2,
  up: 1,
  paused: 0,
};

export function severity(s: Status): number {
  return SEVERITY[s] ?? SEVERITY.unknown;
}

export function compareSeverity(a: Status, b: Status): number {
  return severity(b) - severity(a);
}

/** Worst status among `statuses`, ignoring paused unless every member is paused. */
export function worstStatus(statuses: Status[]): Status {
  if (statuses.length === 0) return 'unknown';
  const active = statuses.filter((s) => s !== 'paused');
  if (active.length === 0) return 'paused';
  return active.reduce((w, s) => (severity(s) > severity(w) ? s : w), active[0]);
}

/** A problem is down/warning; `unreachable` is not (its cause is the parent). */
export function isProblem(s: Status): boolean {
  return s === 'down' || s === 'warning';
}

/** Acknowledge is allowed only for down/warning (409 not_acknowledgeable otherwise). */
export function canAcknowledge(s: { status: Status }): boolean {
  return s.status === 'down' || s.status === 'warning';
}

/** Display order of status counters/tiles/bars (problems first, up and paused last). */
export const COUNT_ORDER: Status[] = ['down', 'warning', 'unreachable', 'unknown', 'paused', 'up'];

/** Tailwind classes per status: the `status-*` design tokens (tailwind.config.ts). */
export const STATUS_DOT: Record<Status, string> = {
  up: 'bg-status-up',
  warning: 'bg-status-warning',
  down: 'bg-status-down',
  paused: 'bg-status-paused',
  unknown: 'bg-status-unknown',
  unreachable: 'bg-status-unreachable',
};

export const STATUS_TEXT: Record<Status, string> = {
  up: 'text-status-up',
  warning: 'text-status-warning',
  down: 'text-status-down',
  paused: 'text-status-paused',
  unknown: 'text-status-unknown',
  unreachable: 'text-status-unreachable',
};

/** Solid ground (count pills): the status colour with `on-status` text. */
export const STATUS_BADGE: Record<Status, string> = {
  up: 'bg-status-up text-on-status',
  warning: 'bg-status-warning text-on-status',
  down: 'bg-status-down text-on-status',
  paused: 'bg-status-paused text-on-status',
  unknown: 'bg-status-unknown text-on-status',
  unreachable: 'bg-status-unreachable text-on-status',
};

/** Soft ground (status badges, problem tiles): the `-soft` token with the status colour as text. */
export const STATUS_SOFT: Record<Status, string> = {
  up: 'bg-status-up-soft text-status-up',
  warning: 'bg-status-warning-soft text-status-warning',
  down: 'bg-status-down-soft text-status-down',
  paused: 'bg-status-paused-soft text-status-paused',
  unknown: 'bg-status-unknown-soft text-status-unknown',
  unreachable: 'bg-status-unreachable-soft text-status-unreachable',
};

/**
 * Literal colours for ECharts (it cannot read CSS variables). Converted to the tokens with the
 * chart components (design-system IMPLEMENTATION.md step 3).
 */
export const STATUS_HEX: Record<Status, string> = {
  up: '#16a34a',
  warning: '#f59e0b',
  down: '#dc2626',
  paused: '#9ca3af',
  unknown: '#64748b',
  unreachable: '#8b5cf6',
};

export function statusClass(s: string, map: Record<Status, string>): string {
  return (map as Record<string, string>)[s] ?? map.unknown;
}

export function emptyCounts(): StatusCounts {
  return { up: 0, warning: 0, down: 0, paused: 0, unknown: 0, unreachable: 0, total: 0 };
}

export function countStatuses(statuses: Status[]): StatusCounts {
  const c = emptyCounts();
  for (const s of statuses) {
    c[s] += 1;
    c.total += 1;
  }
  return c;
}

/** "Run now" is refused (409 sensor_paused) for a paused sensor or a sensor of a paused device. */
export function canRun(s: { enabled: boolean; status: Status }): boolean {
  return s.enabled && s.status !== 'paused';
}
