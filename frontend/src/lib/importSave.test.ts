// [profiles] Discovery import: save SNMP / SSH per host (unticked by default, owner decision
// 1.6.0), column select-all / clear-all, and the inheritance hints from the server verdicts.
import { describe, expect, it } from 'vitest';
import {
  canSave,
  headerState,
  headerToggle,
  inheritCounts,
  inheritHint,
  inheritIndex,
  isSaved,
  NO_SAVE,
  saveFields,
  saveWhereNeeded,
  setAll,
  setOne,
} from './importSave';
import type { ScanInheritance } from '../api/types';

const a = { id: 1, snmp_ok: true, ssh_ok: true };
const b = { id: 2, snmp_ok: true, ssh_ok: false };
const c = { id: 3, snmp_ok: false, ssh_ok: undefined };
const rows = [a, b, c];

const origin = {
  source: 'group',
  group_id: 1,
  group_name: 'Root',
  mode: 'manual',
  profile_id: null,
  profile_name: null,
} as const;
const verdicts: ScanInheritance = {
  group_id: 7,
  snmp_origin: origin,
  ssh_origin: { ...origin, source: 'none', mode: null, group_id: null, group_name: null },
  items: [
    { result_id: 1, snmp: 'differs', ssh: 'same' },
    { result_id: 2, snmp: 'same', ssh: null },
  ],
};

describe('import save choices', () => {
  it('defaults to OFF everywhere; disabled where nothing worked', () => {
    const on = NO_SAVE();
    expect(isSaved(on, a, 'snmp')).toBe(false);
    expect(isSaved(on, a, 'ssh')).toBe(false);
    expect(isSaved(on, b, 'snmp')).toBe(false);
    expect(canSave(a, 'snmp')).toBe(true);
    expect(canSave(b, 'ssh')).toBe(false);
    expect(canSave(c, 'snmp')).toBe(false);
    expect(canSave(c, 'ssh')).toBe(false);
  });

  it('header is tri-state over the eligible hosts only', () => {
    let on = NO_SAVE();
    expect(headerState(on, rows, 'snmp')).toBe('none');
    on = setOne(on, 2, 'snmp', true);
    expect(headerState(on, rows, 'snmp')).toBe('mixed');
    on = setOne(on, 1, 'snmp', true);
    expect(headerState(on, rows, 'snmp')).toBe('all');
    expect(headerState(on, [c], 'snmp')).toBe('disabled');
    expect(headerState(NO_SAVE(), [b, c], 'ssh')).toBe('disabled');
  });

  it('select-all / clear-all touch one column only; mixed → all on', () => {
    let on = setOne(NO_SAVE(), 2, 'snmp', true);
    expect(headerToggle(headerState(on, rows, 'snmp'))).toBe(true);
    on = setAll(on, rows, 'snmp', true);
    expect(headerState(on, rows, 'snmp')).toBe('all');
    expect(on.snmp.has(3)).toBe(false); // SNMP did not answer: never ticked
    on = setAll(on, rows, 'snmp', false);
    expect(headerState(on, rows, 'snmp')).toBe('none');
    expect(headerState(on, rows, 'ssh')).toBe('none');
    expect(headerToggle('all')).toBe(false);
    expect(headerToggle('none')).toBe(true);
  });

  it('select-all over a filtered list keeps the other hosts', () => {
    let on = setOne(NO_SAVE(), 2, 'snmp', true);
    on = setAll(on, [a], 'snmp', true);
    expect(isSaved(on, a, 'snmp')).toBe(true);
    on = setAll(on, [a], 'snmp', false);
    expect(isSaved(on, a, 'snmp')).toBe(false);
    expect(isSaved(on, b, 'snmp')).toBe(true);
  });

  it('import items carry explicit values only where a credential worked (false by default)', () => {
    expect(saveFields(NO_SAVE(), a)).toEqual({ save_snmp: false, save_ssh: false });
    const on = setOne(NO_SAVE(), 1, 'ssh', true);
    expect(saveFields(on, a)).toEqual({ save_snmp: false, save_ssh: true });
    expect(saveFields(on, b)).toEqual({ save_snmp: false });
    expect(saveFields(on, c)).toEqual({});
  });
});

describe('inheritance hints', () => {
  const idx = inheritIndex(verdicts);

  it('per host: risk when unticked and the group differs, same, saved, unknown, none', () => {
    const on = NO_SAVE();
    expect(inheritHint(on, a, 'snmp', idx)).toBe('risk');
    expect(inheritHint(on, a, 'ssh', idx)).toBe('same');
    expect(inheritHint(on, b, 'snmp', idx)).toBe('same');
    expect(inheritHint(on, b, 'ssh', idx)).toBe('none');
    expect(inheritHint(on, c, 'snmp', idx)).toBe('none');
    expect(inheritHint(setOne(on, 1, 'snmp', true), a, 'snmp', idx)).toBe('saved');
    expect(inheritHint(on, a, 'snmp', inheritIndex(undefined))).toBe('unknown');
  });

  it('summary counts and "save where needed" ticks exactly the hosts at risk', () => {
    const on = NO_SAVE();
    expect(inheritCounts(on, rows, 'snmp', idx)).toEqual({
      saved: 0,
      same: 1,
      risk: 1,
      unknown: 0,
    });
    const fixed = saveWhereNeeded(on, rows, 'snmp', idx);
    expect([...fixed.snmp]).toEqual([1]);
    expect(inheritCounts(fixed, rows, 'snmp', idx)).toEqual({
      saved: 1,
      same: 1,
      risk: 0,
      unknown: 0,
    });
    expect(fixed.ssh.size).toBe(0);
  });
});
