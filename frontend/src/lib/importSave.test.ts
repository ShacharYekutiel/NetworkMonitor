// [profiles] Discovery import: save SNMP / SSH per host, column select-all / clear-all.
import { describe, expect, it } from 'vitest';
import {
  canSave,
  headerState,
  headerToggle,
  isSaved,
  NO_SAVE_OFF,
  saveFields,
  setAll,
  setOne,
} from './importSave';

const a = { id: 1, snmp_ok: true, ssh_ok: true };
const b = { id: 2, snmp_ok: true, ssh_ok: false };
const c = { id: 3, snmp_ok: false, ssh_ok: undefined };
const rows = [a, b, c];

describe('import save choices', () => {
  it('defaults to on where a credential worked, disabled where none', () => {
    const off = NO_SAVE_OFF();
    expect(isSaved(off, a, 'snmp')).toBe(true);
    expect(isSaved(off, a, 'ssh')).toBe(true);
    expect(isSaved(off, b, 'ssh')).toBe(false);
    expect(canSave(c, 'snmp')).toBe(false);
    expect(canSave(c, 'ssh')).toBe(false);
  });

  it('header is tri-state over the eligible hosts only', () => {
    let off = NO_SAVE_OFF();
    expect(headerState(off, rows, 'snmp')).toBe('all');
    off = setOne(off, 2, 'snmp', false);
    expect(headerState(off, rows, 'snmp')).toBe('mixed');
    off = setOne(off, 1, 'snmp', false);
    expect(headerState(off, rows, 'snmp')).toBe('none');
    expect(headerState(off, [c], 'snmp')).toBe('disabled');
    expect(headerState(NO_SAVE_OFF(), [b, c], 'ssh')).toBe('disabled');
  });

  it('select-all / clear-all touch one column only; mixed → all on', () => {
    let off = setOne(NO_SAVE_OFF(), 2, 'snmp', false);
    expect(headerToggle(headerState(off, rows, 'snmp'))).toBe(true);
    off = setAll(off, rows, 'snmp', true);
    expect(headerState(off, rows, 'snmp')).toBe('all');
    off = setAll(off, rows, 'snmp', false);
    expect(headerState(off, rows, 'snmp')).toBe('none');
    expect(headerState(off, rows, 'ssh')).toBe('all');
    expect(headerToggle('all')).toBe(false);
    expect(headerToggle('none')).toBe(true);
  });

  it('select-all over a filtered list keeps the other hosts', () => {
    let off = setOne(NO_SAVE_OFF(), 2, 'snmp', false);
    off = setAll(off, [a], 'snmp', false);
    expect(isSaved(off, a, 'snmp')).toBe(false);
    off = setAll(off, [a], 'snmp', true);
    expect(isSaved(off, a, 'snmp')).toBe(true);
    expect(isSaved(off, b, 'snmp')).toBe(false);
  });

  it('import items carry explicit values only where a credential worked', () => {
    const off = setOne(NO_SAVE_OFF(), 1, 'ssh', false);
    expect(saveFields(off, a)).toEqual({ save_snmp: true, save_ssh: false });
    expect(saveFields(off, b)).toEqual({ save_snmp: true });
    expect(saveFields(off, c)).toEqual({});
  });
});
