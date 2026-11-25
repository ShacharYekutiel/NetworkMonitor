// [profiles] Discovery import: "save the SNMP / SSH settings that worked" per device. Pure.
// The state holds the hosts the user switched OFF, so every host with a working credential is
// on by default (api.md § Discovery: omitted = true when SNMP answered / SSH logged in).
import type { DiscoveryResult, ImportItem } from '../api/types';

export type SaveKind = 'snmp' | 'ssh';
export type SaveOff = Record<SaveKind, Set<number>>;
export type HeaderState = 'all' | 'none' | 'mixed' | 'disabled';

export const NO_SAVE_OFF = (): SaveOff => ({ snmp: new Set(), ssh: new Set() });

type R = Pick<DiscoveryResult, 'id' | 'snmp_ok' | 'ssh_ok'>;

/** A working credential was found for this kind (else the checkbox is disabled). */
export function canSave(r: R, kind: SaveKind): boolean {
  return kind === 'snmp' ? r.snmp_ok : !!r.ssh_ok;
}

export function isSaved(off: SaveOff, r: R, kind: SaveKind): boolean {
  return canSave(r, kind) && !off[kind].has(r.id);
}

/** The column header checkbox over `rows` (the selectable visible hosts). */
export function headerState(off: SaveOff, rows: R[], kind: SaveKind): HeaderState {
  const eligible = rows.filter((r) => canSave(r, kind));
  if (!eligible.length) return 'disabled';
  const on = eligible.filter((r) => !off[kind].has(r.id)).length;
  if (on === eligible.length) return 'all';
  return on === 0 ? 'none' : 'mixed';
}

/** Select-all / clear-all of one column (other hosts keep their choice). */
export function setAll(off: SaveOff, rows: R[], kind: SaveKind, on: boolean): SaveOff {
  const next = new Set(off[kind]);
  for (const r of rows)
    if (canSave(r, kind)) {
      if (on) next.delete(r.id);
      else next.add(r.id);
    }
  return { ...off, [kind]: next };
}

export function setOne(off: SaveOff, id: number, kind: SaveKind, on: boolean): SaveOff {
  const next = new Set(off[kind]);
  if (on) next.delete(id);
  else next.add(id);
  return { ...off, [kind]: next };
}

/** Clicking a header: mixed or none → all on; all → all off. */
export function headerToggle(state: HeaderState): boolean {
  return state !== 'all';
}

/** The import item keys: an explicit value only where a credential worked. */
export function saveFields(off: SaveOff, r: R): Pick<ImportItem, 'save_snmp' | 'save_ssh'> {
  const out: Pick<ImportItem, 'save_snmp' | 'save_ssh'> = {};
  if (canSave(r, 'snmp')) out.save_snmp = isSaved(off, r, 'snmp');
  if (canSave(r, 'ssh')) out.save_ssh = isSaved(off, r, 'ssh');
  return out;
}
