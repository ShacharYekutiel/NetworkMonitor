// [if-noise] Sensor event noise: debounce / flapping settings, important-port criteria and the
// apply-template port filters. Pure helpers (unit-tested in noise.test.ts).
import type {
  ApplyInterface,
  Device,
  ImportantCriterion,
  NmEvent,
  Sensor,
  SensorTypeDef,
} from '../api/types';
import { IMPORTANT_CRITERIA } from '../api/types';
import { intInRange } from './ingestSettings';

/** Seconds a new status must hold before it is shown (backend hold_seconds). */
export function holdSeconds(intervalS: number, polls: number, minS: number): number {
  const p = Math.max(1, Math.trunc(polls) || 1);
  return Math.max((p + 0.5) * Math.max(1, intervalS), Math.max(0, minS || 0));
}

export type NoiseForm = {
  debounce: boolean;
  polls: string;
  minS: string;
  flap: boolean;
  threshold: string;
  windowMin: string;
  quietMin: string;
};

export function noiseFormErrors(f: NoiseForm): Record<string, boolean> {
  return {
    polls: !intInRange(f.polls, 1, 10),
    minS: !intInRange(f.minS, 0, 3600),
    threshold: !intInRange(f.threshold, 3, 50),
    window: !intInRange(f.windowMin, 1, 120),
    quiet: !intInRange(f.quietMin, 1, 1440),
  };
}

/** The pre-upgrade meaning ("a port with a critical interface sensor is important") is on. */
export function legacyCriteria(c: ImportantCriterion[] | undefined): boolean {
  return (c ?? []).includes('critical_sensor');
}

/** Criteria in the canonical order without duplicates. */
export function normalizeCriteria(c: Iterable<ImportantCriterion>): ImportantCriterion[] {
  const set = new Set(c);
  return IMPORTANT_CRITERIA.filter((x) => set.has(x));
}

/** A hidden status-change row (debounced / flapping / still in its hold). */
export function isHiddenEvent(e: Pick<NmEvent, 'noise'>): boolean {
  return e.noise !== null && e.noise !== undefined;
}

// ---------------------------------------------------------------------------- apply filters

export type PortMode = 'any' | 'access' | 'trunk';
export type NeighborFilter = 'any' | 'with' | 'without';
export type PortFilter = {
  includeVlans: string;
  excludeVlans: string;
  /** case-insensitive regular expression on the port description (ifAlias) */
  descrInclude: string;
  descrExclude: string;
  mode: PortMode;
  neighbor: NeighborFilter;
};
export const EMPTY_FILTER: PortFilter = {
  includeVlans: '',
  excludeVlans: '',
  descrInclude: '',
  descrExclude: '',
  mode: 'any',
  neighbor: 'any',
};

/** "10, 20-22 30" -> [10, 20, 21, 22, 30]; null when a token is not a VLAN (1–4094). */
export function parseVlans(s: string): number[] | null {
  const out = new Set<number>();
  for (const tok of s.split(/[\s,;]+/).filter(Boolean)) {
    const m = /^(\d{1,4})(?:-(\d{1,4}))?$/.exec(tok);
    if (!m) return null;
    const a = Number(m[1]);
    const b = m[2] === undefined ? a : Number(m[2]);
    if (a < 1 || b > 4094 || b < a || b - a > 4094) return null;
    for (let v = a; v <= b; v++) out.add(v);
  }
  return [...out].sort((x, y) => x - y);
}

function safeRegex(s: string): RegExp | null | undefined {
  if (!s.trim()) return undefined;
  try {
    return new RegExp(s.trim(), 'i');
  } catch {
    return null;
  }
}

export type FilterErrors = {
  includeVlans: boolean;
  excludeVlans: boolean;
  descrInclude: boolean;
  descrExclude: boolean;
};

export function filterErrors(f: PortFilter): FilterErrors {
  return {
    includeVlans: parseVlans(f.includeVlans) === null,
    excludeVlans: parseVlans(f.excludeVlans) === null,
    descrInclude: safeRegex(f.descrInclude) === null,
    descrExclude: safeRegex(f.descrExclude) === null,
  };
}

export function filterActive(f: PortFilter): boolean {
  return (
    !!f.includeVlans.trim() ||
    !!f.excludeVlans.trim() ||
    !!f.descrInclude.trim() ||
    !!f.descrExclude.trim() ||
    f.mode !== 'any' ||
    f.neighbor !== 'any'
  );
}

/** Every VLAN a port carries (untagged, tagged, PVID). */
export function portVlans(i: ApplyInterface): number[] {
  const s = new Set<number>([...(i.untagged_vlans ?? []), ...(i.tagged_vlans ?? [])]);
  if (i.pvid !== null && i.pvid !== undefined) s.add(i.pvid);
  return [...s].sort((a, b) => a - b);
}

/** The interface list carries port inventory data (VLANs / mode / neighbours). */
export function hasPortData(items: ApplyInterface[]): boolean {
  return (
    items.some((i) => i.mode !== undefined && i.mode !== null) ||
    items.some((i) => i.has_neighbor !== undefined && i.has_neighbor !== null)
  );
}

/**
 * Whether a port passes the filter. A port without the data a condition needs (no VLANs, mode or
 * neighbour info) fails that condition, except "exclude" conditions, which it passes.
 */
export function portPasses(i: ApplyInterface, f: PortFilter): boolean {
  const vl = portVlans(i);
  const inc = parseVlans(f.includeVlans) ?? [];
  if (inc.length && !vl.some((v) => inc.includes(v))) return false;
  const exc = parseVlans(f.excludeVlans) ?? [];
  if (exc.length && vl.some((v) => exc.includes(v))) return false;
  const di = safeRegex(f.descrInclude);
  if (di && !di.test(i.alias || '')) return false;
  const de = safeRegex(f.descrExclude);
  if (de && de.test(i.alias || '')) return false;
  if (f.mode !== 'any' && i.mode !== f.mode) return false;
  if (f.neighbor === 'with' && i.has_neighbor !== true) return false;
  if (f.neighbor === 'without' && i.has_neighbor !== false) return false;
  return true;
}

/** The ifIndexes that pass (the "Choose ports" selection the filter produces). */
export function filteredPick(items: ApplyInterface[], f: PortFilter): number[] {
  return items.filter((i) => portPasses(i, f)).map((i) => i.if_index);
}

/** Preview: how many ports will get / not get a sensor (null cover = every port). */
export function previewCounts(
  items: ApplyInterface[],
  cover: ReadonlySet<number> | null,
): { will: number; wont: number } {
  const will = cover === null ? items.length : items.filter((i) => cover.has(i.if_index)).length;
  return { will, wont: items.length - will };
}

// ---------------------------------------------------------------------------- device SNMP source

export type SnmpSourceInfo = {
  source: Device['effective_snmp_source'];
  groupId: number | null;
  /** the built-in default (v2c, public) while the device has SNMP sensors */
  warn: boolean;
};

export function snmpSourceInfo(
  d: Pick<Device, 'effective_snmp_source' | 'effective_snmp_group_id'>,
  sensors: Pick<Sensor, 'type'>[],
  types: Pick<SensorTypeDef, 'type' | 'requires_snmp'>[] | undefined,
): SnmpSourceInfo {
  const snmpTypes = new Set((types ?? []).filter((x) => x.requires_snmp).map((x) => x.type));
  const hasSnmp = sensors.some((s) => snmpTypes.has(s.type));
  return {
    source: d.effective_snmp_source,
    groupId: d.effective_snmp_group_id ?? null,
    warn: d.effective_snmp_source === 'default' && hasSnmp,
  };
}
