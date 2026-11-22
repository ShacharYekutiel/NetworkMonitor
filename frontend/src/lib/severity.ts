// [severity] Alert severity helpers (api.md "[severity] Alert severity"). Pure.
import type { SensorImpact } from '../api/types';
import {
  SEVERITIES,
  type Severity,
  type SeverityMapping,
  type SeverityMappingKey,
} from '../api/severity';

/** critical 5 … info 1; unknown / null 0 (sorted last), like the backend's RANK. */
export function severityRank(s: string | null | undefined): number {
  const i = SEVERITIES.indexOf(s as Severity);
  return i < 0 ? 0 : SEVERITIES.length - i;
}

export function isSeverity(s: unknown): s is Severity {
  return typeof s === 'string' && (SEVERITIES as string[]).includes(s);
}

/** Valid levels of a csv (`?severity=critical,major`), deduplicated, in level order. */
export function parseSeverities(csv: string | null | undefined): Severity[] {
  const set = new Set((csv ?? '').split(',').map((x) => x.trim()));
  return SEVERITIES.filter((s) => set.has(s));
}

/** The csv of a set of levels in level order ('' for none). */
export function severityCsv(list: readonly Severity[]): string {
  return SEVERITIES.filter((s) => list.includes(s)).join(',');
}

/** Adds or removes one level of a selection (kept in level order). */
export function toggleSeverity(list: readonly Severity[], s: Severity): Severity[] {
  return list.includes(s)
    ? SEVERITIES.filter((x) => x !== s && list.includes(x))
    : SEVERITIES.filter((x) => x === s || list.includes(x));
}

/** The mapping row of a problem: `<status>_<impact>`. */
export function mappingKey(status: 'down' | 'warning', impact: SensorImpact): SeverityMappingKey {
  return `${status}_${impact}` as SeverityMappingKey;
}

/** What "Automatic" resolves to for a sensor in that status (the mapping step; no override). */
export function automaticSeverity(
  mapping: SeverityMapping | null | undefined,
  status: 'down' | 'warning',
  impact: SensorImpact,
): Severity | null {
  return mapping ? mapping[mappingKey(status, impact)] : null;
}

/** The highest of a list of levels (null when none). */
export function highestSeverity(list: (string | null | undefined)[]): Severity | null {
  let best: Severity | null = null;
  for (const s of list) if (isSeverity(s) && severityRank(s) > severityRank(best)) best = s;
  return best;
}
