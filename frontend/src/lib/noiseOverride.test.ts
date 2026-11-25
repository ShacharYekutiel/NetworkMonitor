import { describe, expect, it } from 'vitest';
import type { EffectiveEventNoise } from '../api/types';
import {
  BULK_KEEP,
  INHERIT_ALL,
  bulkNoiseErrors,
  bulkNoiseInput,
  decidedByOverride,
  isOverridden,
  noiseFormErrors,
  noiseFormFrom,
  noiseFormToInput,
} from './noiseOverride';

const g = <T>(value: T) => ({
  value,
  source: 'global' as const,
  source_id: null,
  source_name: null,
});

describe('[if-noise] override form', () => {
  it('round-trips the stored override and sends only what changed', () => {
    const stored = {
      debounce: 'off' as const,
      flap: 'inherit' as const,
      debounce_polls: 3,
      debounce_min_s: null,
    };
    const f = noiseFormFrom(stored);
    expect(f).toEqual({ debounce: 'off', flap: 'inherit', polls: '3', minS: '' });
    expect(noiseFormToInput(f, stored)).toBeUndefined();
    expect(noiseFormToInput({ ...f, flap: 'on' }, stored)).toEqual({ flap: 'on' });
    // emptying a value means inherit (null)
    expect(noiseFormToInput({ ...f, polls: '' }, stored)).toEqual({ debounce_polls: null });
    expect(noiseFormToInput({ ...f, minS: '0' }, stored)).toEqual({ debounce_min_s: 0 });
    // no stored value (new item): compared with inherit everywhere
    expect(noiseFormFrom(undefined)).toEqual({
      debounce: 'inherit',
      flap: 'inherit',
      polls: '',
      minS: '',
    });
    expect(noiseFormToInput(noiseFormFrom(undefined), undefined)).toBeUndefined();
  });

  it('validates the values', () => {
    const f = noiseFormFrom(INHERIT_ALL);
    expect(noiseFormErrors({ ...f, polls: '0' }).polls).toBe(true);
    expect(noiseFormErrors({ ...f, polls: '11' }).polls).toBe(true);
    expect(noiseFormErrors({ ...f, polls: '2.5' }).polls).toBe(true);
    expect(noiseFormErrors({ ...f, polls: ' 10 ' }).polls).toBe(false);
    expect(noiseFormErrors({ ...f, minS: '3601' }).minS).toBe(true);
    expect(noiseFormErrors({ ...f, minS: '-1' }).minS).toBe(true);
    expect(noiseFormErrors({ ...f, minS: '0' }).minS).toBe(false);
    expect(noiseFormToInput({ ...f, polls: 'x' }, INHERIT_ALL)).toBeUndefined();
  });

  it('isOverridden and the keys some level decides', () => {
    expect(isOverridden(INHERIT_ALL)).toBe(false);
    expect(isOverridden(undefined)).toBe(false);
    expect(isOverridden({ ...INHERIT_ALL, debounce_min_s: 0 })).toBe(true);
    expect(isOverridden({ ...INHERIT_ALL, flap: 'off' })).toBe(true);
    const eff: EffectiveEventNoise = {
      debounce: g(true),
      flap: { value: false, source: 'group', source_id: 4, source_name: 'Floor 2' },
      debounce_polls: g(1),
      debounce_min_s: { value: 60, source: 'sensor', source_id: 9, source_name: 'Gi1/0/1' },
    };
    expect(decidedByOverride(eff).map((d) => d.key)).toEqual(['flap', 'debounce_min_s']);
    expect(decidedByOverride(null)).toEqual([]);
  });
});

describe('[if-noise] bulk form', () => {
  it('sends only the chosen keys; inherit clears a value', () => {
    expect(bulkNoiseInput(BULK_KEEP)).toBeNull();
    expect(bulkNoiseInput({ ...BULK_KEEP, flap: 'off' })).toEqual({ flap: 'off' });
    expect(
      bulkNoiseInput({
        ...BULK_KEEP,
        debounce: 'inherit',
        polls: { mode: 'set', value: '4' },
        minS: { mode: 'inherit', value: '' },
      }),
    ).toEqual({ debounce: 'inherit', debounce_polls: 4, debounce_min_s: null });
  });

  it('a custom value must be valid', () => {
    const f = { ...BULK_KEEP, polls: { mode: 'set' as const, value: '' } };
    expect(bulkNoiseErrors(f).polls).toBe(true);
    expect(bulkNoiseInput(f)).toBeNull();
    expect(bulkNoiseErrors({ ...BULK_KEEP, minS: { mode: 'set', value: '4000' } }).minS).toBe(true);
    expect(bulkNoiseErrors({ ...BULK_KEEP, minS: { mode: 'keep', value: 'junk' } }).minS).toBe(
      false,
    );
  });
});
