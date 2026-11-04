import type { Status, StatusCounts } from '../api/types';

export const STATUSES: Status[] = ['down', 'warning', 'unknown', 'up', 'paused'];

/** Severity per data-model.md: down > warning > unknown > up > paused. */
export const SEVERITY: Record<Status, number> = {
  down: 4,
  warning: 3,
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

export function isProblem(s: Status): boolean {
  return s === 'down' || s === 'warning';
}

/** Tailwind classes per status (conventions.md). */
export const STATUS_DOT: Record<Status, string> = {
  up: 'bg-green-600',
  warning: 'bg-amber-500',
  down: 'bg-red-600',
  paused: 'bg-gray-400',
  unknown: 'bg-slate-500',
};

export const STATUS_TEXT: Record<Status, string> = {
  up: 'text-green-600 dark:text-green-500',
  warning: 'text-amber-500',
  down: 'text-red-600 dark:text-red-500',
  paused: 'text-gray-400',
  unknown: 'text-slate-500 dark:text-slate-400',
};

export const STATUS_BADGE: Record<Status, string> = {
  up: 'bg-green-600 text-white',
  warning: 'bg-amber-500 text-white',
  down: 'bg-red-600 text-white',
  paused: 'bg-gray-400 text-white',
  unknown: 'bg-slate-500 text-white',
};

export const STATUS_HEX: Record<Status, string> = {
  up: '#16a34a',
  warning: '#f59e0b',
  down: '#dc2626',
  paused: '#9ca3af',
  unknown: '#64748b',
};

export function statusClass(s: string, map: Record<Status, string>): string {
  return (map as Record<string, string>)[s] ?? map.unknown;
}

export function emptyCounts(): StatusCounts {
  return { up: 0, warning: 0, down: 0, paused: 0, unknown: 0, total: 0 };
}

export function countStatuses(statuses: Status[]): StatusCounts {
  const c = emptyCounts();
  for (const s of statuses) {
    c[s] += 1;
    c.total += 1;
  }
  return c;
}
