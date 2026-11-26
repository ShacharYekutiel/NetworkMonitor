// [profiles] Discovery import: "save the SNMP / SSH settings that worked" per device. Pure.
// Owner decision (1.6.0): nothing is saved by default — the state holds the hosts the user
// switched ON, and the API's omitted / false means "inherit from the import group" (api.md
// § Discovery). Whether inheritance gives the credential that worked comes from the server
// (GET …/inheritance): `inheritRisk` / `inheritCounts` turn it into the per-host and summary hints.
import type { DiscoveryResult, ImportItem, InheritVerdict, ScanInheritance } from '../api/types';

export type SaveKind = 'snmp' | 'ssh';
export type SaveOn = Record<SaveKind, Set<number>>;
export type HeaderState = 'all' | 'none' | 'mixed' | 'disabled';

export const NO_SAVE = (): SaveOn => ({ snmp: new Set(), ssh: new Set() });

type R = Pick<DiscoveryResult, 'id' | 'snmp_ok' | 'ssh_ok'>;

/** A working credential was found for this kind (else the checkbox is disabled). */
export function canSave(r: R, kind: SaveKind): boolean {
  return kind === 'snmp' ? r.snmp_ok : !!r.ssh_ok;
}

export function isSaved(on: SaveOn, r: R, kind: SaveKind): boolean {
  return canSave(r, kind) && on[kind].has(r.id);
}

/** The column header checkbox over `rows` (the selectable visible hosts). */
export function headerState(on: SaveOn, rows: R[], kind: SaveKind): HeaderState {
  const eligible = rows.filter((r) => canSave(r, kind));
  if (!eligible.length) return 'disabled';
  const n = eligible.filter((r) => on[kind].has(r.id)).length;
  if (n === eligible.length) return 'all';
  return n === 0 ? 'none' : 'mixed';
}

/** Select-all / clear-all of one column (other hosts keep their choice). */
export function setAll(on: SaveOn, rows: R[], kind: SaveKind, value: boolean): SaveOn {
  const next = new Set(on[kind]);
  for (const r of rows)
    if (canSave(r, kind)) {
      if (value) next.add(r.id);
      else next.delete(r.id);
    }
  return { ...on, [kind]: next };
}

export function setOne(on: SaveOn, id: number, kind: SaveKind, value: boolean): SaveOn {
  const next = new Set(on[kind]);
  if (value) next.add(id);
  else next.delete(id);
  return { ...on, [kind]: next };
}

/** Clicking a header: mixed or none → all on; all → all off. */
export function headerToggle(state: HeaderState): boolean {
  return state !== 'all';
}

/** The import item keys: an explicit value only where a credential worked. */
export function saveFields(on: SaveOn, r: R): Pick<ImportItem, 'save_snmp' | 'save_ssh'> {
  const out: Pick<ImportItem, 'save_snmp' | 'save_ssh'> = {};
  if (canSave(r, 'snmp')) out.save_snmp = isSaved(on, r, 'snmp');
  if (canSave(r, 'ssh')) out.save_ssh = isSaved(on, r, 'ssh');
  return out;
}

export type InheritIndex = Map<number, { snmp: InheritVerdict; ssh: InheritVerdict }>;

export function inheritIndex(data: ScanInheritance | undefined): InheritIndex {
  return new Map((data?.items ?? []).map((i) => [i.result_id, { snmp: i.snmp, ssh: i.ssh }]));
}

/**
 * What the import will do for one host and kind:
 * - `none`: nothing worked, nothing to save;
 * - `saved`: the box is ticked, the device gets what worked;
 * - `same`: not saved, and inheritance from the group gives the same credential (nothing to do);
 * - `risk`: not saved, and inheritance gives something else: the device will probably not answer;
 * - `unknown`: not saved, the server's verdict is not there (yet).
 */
export type InheritHint = 'none' | 'saved' | 'same' | 'risk' | 'unknown';

export function inheritHint(on: SaveOn, r: R, kind: SaveKind, idx: InheritIndex): InheritHint {
  if (!canSave(r, kind)) return 'none';
  if (isSaved(on, r, kind)) return 'saved';
  const v = idx.get(r.id)?.[kind];
  if (v === 'same') return 'same';
  if (v === 'differs') return 'risk';
  return 'unknown';
}

export type InheritCounts = Record<Exclude<InheritHint, 'none'>, number>;

/** Summary over the hosts to import (before the import). */
export function inheritCounts(
  on: SaveOn,
  rows: R[],
  kind: SaveKind,
  idx: InheritIndex,
): InheritCounts {
  const out: InheritCounts = { saved: 0, same: 0, risk: 0, unknown: 0 };
  for (const r of rows) {
    const h = inheritHint(on, r, kind, idx);
    if (h !== 'none') out[h] += 1;
  }
  return out;
}

/** Tick the box for exactly the hosts at risk (the "save where needed" action). */
export function saveWhereNeeded(on: SaveOn, rows: R[], kind: SaveKind, idx: InheritIndex): SaveOn {
  const next = new Set(on[kind]);
  for (const r of rows) if (inheritHint(on, r, kind, idx) === 'risk') next.add(r.id);
  return { ...on, [kind]: next };
}
